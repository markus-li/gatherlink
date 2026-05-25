"""
Python-owned adaptive service budget controller.

This module decides *whether* a mixed-service runner should use Rust's small
per-service drain quantum primitive. Rust does not know service meaning. It only
executes a compiled plan such as "drain service fast for 256 packets in this
slot". Python owns the policy, hysteresis, diagnostics, and operator meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gatherlink.config.runtime import RuntimeServiceConfig
from gatherlink.scheduling.service_intent import service_is_bulk, service_is_protected

# These are intentionally code constants for now. They are tuning guardrails,
# not public config surface. The values keep the controller conservative: it
# must observe sustained high-vs-bulk imbalance before touching the hot path.
MIN_SAMPLE_SECONDS = 0.50
PRESSURE_SAMPLES_TO_ACTIVATE = 3
CLEAN_SAMPLES_TO_RELEASE = 5
BULK_DOMINANCE_RATIO = 2.50
BULK_QUANTUM_FLOOR_PACKETS = 64
BULK_QUANTUM_CEILING_PACKETS = 256
BULK_BYTE_BUDGET_EQUIVALENT_PACKETS = 256
BULK_BYTE_BUDGET_MIN_EQUIVALENT_PACKETS = 128
BULK_BYTE_BUDGET_MAX_EQUIVALENT_PACKETS = BULK_BYTE_BUDGET_EQUIVALENT_PACKETS
BULK_BYTE_BUDGET_STEP_EQUIVALENT_PACKETS = 64
BULK_BYTE_BUDGET_ADJUST_SAMPLES = 4
BULK_TIGHTEN_DOMINANCE_RATIO = 8.00
BULK_LOOSEN_DOMINANCE_RATIO = 3.00
PROTECTED_OUTCOME_PACKET_BUDGET = 128
PROTECTED_OUTCOME_BYTE_BUDGET_EQUIVALENT_PACKETS = 64


@dataclass(frozen=True)
class ServiceRateSample:
    """One service's counter deltas converted into rates."""

    service: str
    priority: str
    traffic_class: str
    tx_packets_per_second: float
    tx_bytes_per_second: float


@dataclass(frozen=True)
class ServiceBudgetDecision:
    """Current compiled service-budget recommendation."""

    packet_budget_overrides: dict[str, int]
    changed: bool
    reason: str
    byte_budget_overrides: dict[str, int] = field(default_factory=dict)
    samples: list[ServiceRateSample] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """Return whether any service drain quantum override is active."""
        return bool(self.packet_budget_overrides or self.byte_budget_overrides)


@dataclass(frozen=True)
class ServiceOutcomeSignal:
    """
    Optional Python-owned outcome signal for one service.

    This deliberately stays outside Rust. Helpers, benchmarks, or future
    operator integrations may know facts such as TCP retransmit pain, UDP loss,
    or application-level delivery. The dataplane should not learn those
    meanings; it should only execute the primitive budget Python compiles.
    """

    service: str
    degraded: bool
    reason: str = ""


@dataclass(frozen=True)
class ServiceOutcomeSnapshot:
    """Optional service outcome facts captured by Python-side tooling."""

    outcomes: tuple[ServiceOutcomeSignal, ...] = ()

    @classmethod
    def from_mapping(cls, outcomes: dict[str, bool | str]) -> ServiceOutcomeSnapshot:
        """Build a compact snapshot from simple service-to-state mappings."""
        signals = []
        for service, value in outcomes.items():
            if isinstance(value, str):
                signals.append(ServiceOutcomeSignal(service=service, degraded=True, reason=value))
            else:
                signals.append(ServiceOutcomeSignal(service=service, degraded=bool(value)))
        return cls(tuple(signals))

    def degraded_services(self) -> set[str]:
        """Return service names whose external/runtime outcome is degraded."""
        return {outcome.service for outcome in self.outcomes if outcome.degraded}

    def reason_for(self, service: str) -> str:
        """Return the first diagnostic reason available for a degraded service."""
        for outcome in self.outcomes:
            if outcome.service == service and outcome.degraded and outcome.reason:
                return outcome.reason
        return "protected service outcome degraded"


@dataclass
class _PreviousCounter:
    """Counter snapshot retained between status samples."""

    observed_at: float
    tx_packets: int
    tx_bytes: int


class ServiceBudgetController:
    """
    Compile service-level fairness hints from service counters.

    The first v0.9.2 use case is dual WireGuard: a stable/TCP-like service and
    a fast/UDP-like service can share one Gatherlink runner without forcing the
    fast service to monopolize every bridge burst. The controller only emits a
    plan after sustained evidence; otherwise the runner stays on the legacy
    full-batch hot path.
    """

    def __init__(self) -> None:
        """Create a controller with no active budget."""
        self._previous: dict[str, _PreviousCounter] = {}
        self._pressure_samples = 0
        self._clean_samples = 0
        self._active_overrides: dict[str, int] = {}
        self._active_byte_overrides: dict[str, int] = {}
        self._byte_budget_equivalent_packets = BULK_BYTE_BUDGET_EQUIVALENT_PACKETS
        self._tighten_byte_budget_samples = 0
        self._loosen_byte_budget_samples = 0
        self._last_reason = "not enough samples"

    def update(
        self,
        services: list[RuntimeServiceConfig],
        service_stats: dict[str, dict[str, int]] | None,
        *,
        now: float,
        batch_size: int,
        outcome: ServiceOutcomeSnapshot | None = None,
    ) -> ServiceBudgetDecision:
        """Return a new budget decision from the latest service counters."""
        listening_services = [service for service in services if service.listen]
        if not self._eligible_for_budgeting(listening_services):
            changed = self._set_overrides({}, {}, "not a mixed high-or-critical plus bulk service set")
            return ServiceBudgetDecision({}, changed, self._last_reason, {})
        samples = self._rate_samples(listening_services, service_stats or {}, now)
        if not samples:
            return ServiceBudgetDecision(
                dict(self._active_overrides),
                False,
                "not enough samples",
                dict(self._active_byte_overrides),
            )

        protected_names = {service.name for service in listening_services if service_is_protected(service)}
        bulk_names = {service.name for service in listening_services if service_is_bulk(service)}
        high_rate = sum(sample.tx_bytes_per_second for sample in samples if sample.service in protected_names)
        high_packets = sum(sample.tx_packets_per_second for sample in samples if sample.service in protected_names)
        bulk_rate = sum(sample.tx_bytes_per_second for sample in samples if sample.service in bulk_names)
        bulk_packets = sum(sample.tx_packets_per_second for sample in samples if sample.service in bulk_names)
        protected_outcome_reason = _protected_outcome_reason(listening_services, outcome)
        protected_services = _protected_outcome_services(listening_services, outcome)
        outcome_pressure = protected_outcome_reason is not None and high_rate > 0 and bulk_rate > 0
        pressure = (
            high_rate > 0
            and high_packets > 0
            and bulk_rate >= high_rate * BULK_DOMINANCE_RATIO
            and bulk_packets >= high_packets * BULK_DOMINANCE_RATIO
        )
        if pressure or outcome_pressure:
            self._pressure_samples += 1
            self._clean_samples = 0
        else:
            self._pressure_samples = 0
            self._clean_samples += 1

        if outcome_pressure or self._pressure_samples >= PRESSURE_SAMPLES_TO_ACTIVATE:
            packet_budget = _bulk_packet_budget(batch_size)
            overrides = {service.name: packet_budget for service in listening_services if service.name in bulk_names}
            if outcome_pressure:
                for service_name in protected_services:
                    overrides[service_name] = min(batch_size, PROTECTED_OUTCOME_PACKET_BUDGET)
            if outcome_pressure:
                budget_change_reason = self._apply_protected_outcome_budget(protected_outcome_reason)
            else:
                budget_change_reason = self._adjust_byte_budget_equivalent(high_rate, bulk_rate)
            byte_budget = _bulk_byte_budget(samples, self._byte_budget_equivalent_packets)
            byte_overrides = {
                service.name: byte_budget
                for service in listening_services
                if service.name in bulk_names and byte_budget
            }
            reason = (
                f"{protected_outcome_reason}; protected service degraded while bulk traffic was active"
                if outcome_pressure
                else f"bulk service dominated high-priority traffic for {self._pressure_samples} samples"
            )
            if budget_change_reason:
                reason = f"{reason}; {budget_change_reason}"
            changed = self._set_overrides(
                overrides,
                byte_overrides,
                reason,
            )
            return ServiceBudgetDecision(
                dict(self._active_overrides),
                changed,
                self._last_reason,
                dict(self._active_byte_overrides),
                samples,
            )

        if self._active_overrides and self._clean_samples >= CLEAN_SAMPLES_TO_RELEASE:
            changed = self._set_overrides({}, {}, f"service pressure cleared for {self._clean_samples} samples")
            return ServiceBudgetDecision({}, changed, self._last_reason, {}, samples)

        reason = (
            f"waiting for sustained pressure {self._pressure_samples}/{PRESSURE_SAMPLES_TO_ACTIVATE}"
            if pressure
            else f"no service pressure {self._clean_samples}/{CLEAN_SAMPLES_TO_RELEASE}"
        )
        return ServiceBudgetDecision(
            dict(self._active_overrides),
            False,
            reason,
            dict(self._active_byte_overrides),
            samples,
        )

    @staticmethod
    def _eligible_for_budgeting(services: list[RuntimeServiceConfig]) -> bool:
        """Return whether this service set has intent worth balancing."""
        return any(service_is_bulk(service) for service in services) and any(
            service_is_protected(service) for service in services
        )

    def _rate_samples(
        self,
        services: list[RuntimeServiceConfig],
        service_stats: dict[str, dict[str, int]],
        now: float,
    ) -> list[ServiceRateSample]:
        """Convert monotonic service counters into rates."""
        samples: list[ServiceRateSample] = []
        for service in services:
            counters = service_stats.get(service.name)
            if counters is None:
                continue
            current = _PreviousCounter(
                observed_at=now,
                tx_packets=max(0, int(counters.get("tx_packets", 0) or 0)),
                tx_bytes=max(0, int(counters.get("tx_bytes", 0) or 0)),
            )
            previous = self._previous.get(service.name)
            if previous is None:
                self._previous[service.name] = current
                continue
            elapsed = current.observed_at - previous.observed_at
            if elapsed < MIN_SAMPLE_SECONDS:
                continue
            tx_packets_delta = max(0, current.tx_packets - previous.tx_packets)
            tx_bytes_delta = max(0, current.tx_bytes - previous.tx_bytes)
            self._previous[service.name] = current
            samples.append(
                ServiceRateSample(
                    service=service.name,
                    priority=service.priority,
                    traffic_class=service.traffic_class,
                    tx_packets_per_second=tx_packets_delta / elapsed,
                    tx_bytes_per_second=tx_bytes_delta / elapsed,
                )
            )
        return samples

    def _set_overrides(self, overrides: dict[str, int], byte_overrides: dict[str, int], reason: str) -> bool:
        """Replace active overrides and report whether the compiled plan changed."""
        changed = overrides != self._active_overrides or byte_overrides != self._active_byte_overrides
        self._active_overrides = dict(overrides)
        self._active_byte_overrides = dict(byte_overrides)
        self._last_reason = reason
        if not overrides and not byte_overrides:
            self._pressure_samples = 0
            self._byte_budget_equivalent_packets = BULK_BYTE_BUDGET_EQUIVALENT_PACKETS
            self._tighten_byte_budget_samples = 0
            self._loosen_byte_budget_samples = 0
        return changed

    def _adjust_byte_budget_equivalent(
        self,
        high_rate: float,
        bulk_rate: float,
        *,
        protected_outcome_reason: str | None = None,
    ) -> str | None:
        """
        Adjust the bulk byte budget in small bounded steps.

        This is deliberately based on sustained service counter ratios, not a
        single momentary sample. Python can tighten a bulk service when it keeps
        dwarfing high-priority traffic, or loosen back toward the baseline when
        pressure exists but is no longer extreme.
        """
        if protected_outcome_reason:
            self._tighten_byte_budget_samples = 0
            return self._apply_protected_outcome_budget(protected_outcome_reason)
        if high_rate <= 0:
            return None
        dominance = bulk_rate / high_rate
        previous = self._byte_budget_equivalent_packets
        if dominance >= BULK_TIGHTEN_DOMINANCE_RATIO:
            self._tighten_byte_budget_samples += 1
            self._loosen_byte_budget_samples = 0
            if self._tighten_byte_budget_samples >= BULK_BYTE_BUDGET_ADJUST_SAMPLES:
                self._byte_budget_equivalent_packets = max(
                    BULK_BYTE_BUDGET_MIN_EQUIVALENT_PACKETS,
                    self._byte_budget_equivalent_packets - BULK_BYTE_BUDGET_STEP_EQUIVALENT_PACKETS,
                )
                self._tighten_byte_budget_samples = 0
        elif (
            dominance <= BULK_LOOSEN_DOMINANCE_RATIO
            and self._byte_budget_equivalent_packets < BULK_BYTE_BUDGET_EQUIVALENT_PACKETS
        ):
            self._loosen_byte_budget_samples += 1
            self._tighten_byte_budget_samples = 0
            if self._loosen_byte_budget_samples >= BULK_BYTE_BUDGET_ADJUST_SAMPLES:
                self._byte_budget_equivalent_packets = min(
                    BULK_BYTE_BUDGET_MAX_EQUIVALENT_PACKETS,
                    self._byte_budget_equivalent_packets + BULK_BYTE_BUDGET_STEP_EQUIVALENT_PACKETS,
                )
                self._loosen_byte_budget_samples = 0
        else:
            self._tighten_byte_budget_samples = 0
            self._loosen_byte_budget_samples = 0
        if previous == self._byte_budget_equivalent_packets:
            return None
        direction = "tightened" if self._byte_budget_equivalent_packets < previous else "loosened"
        return f"bulk byte budget {direction} to {self._byte_budget_equivalent_packets} packet-equivalents"

    def _apply_protected_outcome_budget(self, protected_outcome_reason: str | None) -> str:
        """Apply a bounded bulk cap when a protected service reports degradation."""
        self._tighten_byte_budget_samples = 0
        self._loosen_byte_budget_samples = 0
        previous = self._byte_budget_equivalent_packets
        self._byte_budget_equivalent_packets = min(
            self._byte_budget_equivalent_packets,
            PROTECTED_OUTCOME_BYTE_BUDGET_EQUIVALENT_PACKETS,
        )
        prefix = protected_outcome_reason or "protected service outcome degraded"
        if previous != self._byte_budget_equivalent_packets:
            return f"{prefix}; capped bulk byte budget at {self._byte_budget_equivalent_packets} packet-equivalents"
        return f"{prefix}; held bulk byte budget at {self._byte_budget_equivalent_packets} packet-equivalents"


def _bulk_packet_budget(batch_size: int) -> int:
    """Return the conservative bulk-service packet quantum for a runner batch."""
    return max(BULK_QUANTUM_FLOOR_PACKETS, min(BULK_QUANTUM_CEILING_PACKETS, max(1, batch_size // 2)))


def _bulk_byte_budget(samples: list[ServiceRateSample], equivalent_packets: int) -> int:
    """
    Return a byte budget for one bulk service slot.

    The budget is derived from observed high-priority packet size rather than a
    fixed UDP size. This keeps Python's policy byte/time based while Rust only
    receives a simple execution cap.
    """
    high_samples = [
        sample
        for sample in samples
        if sample.priority in {"high", "critical"}
        or sample.traffic_class in {"tcp_ordered", "latency_sensitive", "control"}
    ]
    if not high_samples:
        return 0
    high_bytes = sum(sample.tx_bytes_per_second for sample in high_samples)
    high_packets = sum(sample.tx_packets_per_second for sample in high_samples)
    if high_bytes <= 0 or high_packets <= 0:
        return 0
    high_average_packet_bytes = max(1, int(high_bytes / high_packets))
    return high_average_packet_bytes * equivalent_packets


def _protected_outcome_reason(
    services: list[RuntimeServiceConfig],
    outcome: ServiceOutcomeSnapshot | None,
) -> str | None:
    """Return why a protected service should block more aggressive bulk budgeting."""
    protected_services = _protected_outcome_services(services, outcome)
    if not protected_services or outcome is None:
        return None
    return outcome.reason_for(next(iter(protected_services)))


def _protected_outcome_services(
    services: list[RuntimeServiceConfig],
    outcome: ServiceOutcomeSnapshot | None,
) -> set[str]:
    """Return degraded protected services that should receive burst protection."""
    if outcome is None:
        return set()
    degraded_services = outcome.degraded_services()
    if not degraded_services:
        return set()
    return {service.name for service in services if service_is_protected(service) and service.name in degraded_services}
