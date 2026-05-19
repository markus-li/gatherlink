"""
Scheduler policy classes; Python decides policy, Rust executes compiled state.

The public names here are Python policy names. Several policies intentionally
compile to the same Rust target because Rust should only receive the primitive
behavior it must execute in the packet path.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass

from gatherlink.config.models import PathConfig, SchedulerPolicy
from gatherlink.config.runtime import RuntimePathSchedulerConfig, SchedulerMode
from gatherlink.scheduling.metrics import PathSchedulerMetrics, SchedulerTelemetrySnapshot
from gatherlink.scheduling.scoring import adaptive_weight, capacity_weight
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_IN_FLIGHT_PACKETS = 0
DEFAULT_MAX_IN_FLIGHT_BYTES = 0


@dataclass(frozen=True)
class CompiledPathContext:
    """Input bundle shared by scheduler policies while compiling one path."""

    path: PathConfig
    index: int
    metrics: PathSchedulerMetrics | None = None

    @property
    def state(self) -> str:
        """Return the path state after the enabled flag has been applied."""
        return "disabled" if not self.path.scheduler.enabled else self.path.scheduler.state


class SchedulerPolicyBase(ABC):
    """Base class for Python policies that compile to Rust primitives."""

    rust_mode: SchedulerMode = "weighted_round_robin"

    def compile_path(self, context: CompiledPathContext) -> RuntimePathSchedulerConfig:
        """Compile one configured path into the Rust runtime contract."""
        path = context.path
        return RuntimePathSchedulerConfig(
            path_id=context.index,
            route_id=0,
            enabled=path.scheduler.enabled and context.state != "disabled",
            state=context.state,
            weight=self.weight(context),
            mtu=path.scheduler.mtu,
            tx_capacity_bps=self.tx_capacity_bps(context),
            rx_capacity_bps=self.rx_capacity_bps(context),
            latency_us=self.latency_us(context),
            loss_ppm=self.loss_ppm(context),
            reorder_hold_us=path.scheduler.reorder_hold_us,
            max_in_flight_packets=path.scheduler.max_in_flight_packets or DEFAULT_MAX_IN_FLIGHT_PACKETS,
            max_in_flight_bytes=path.scheduler.max_in_flight_bytes or DEFAULT_MAX_IN_FLIGHT_BYTES,
        )

    def weight(self, context: CompiledPathContext) -> int:
        """Return the compiled weight Rust should execute."""
        return context.path.scheduler.weight

    def tx_capacity_bps(self, context: CompiledPathContext) -> int | None:
        """Return local-view TX capacity, preferring telemetry over startup hints."""
        return _first_int(_metric(context.metrics, "tx_capacity_bps"), context.path.scheduler.tx_capacity_bps)

    def rx_capacity_bps(self, context: CompiledPathContext) -> int | None:
        """Return local-view RX capacity, preferring telemetry over startup hints."""
        return _first_int(_metric(context.metrics, "rx_capacity_bps"), context.path.scheduler.rx_capacity_bps)

    def latency_us(self, context: CompiledPathContext) -> int | None:
        """Return the compact latency value Rust should use for packet selection."""
        return _first_int(_metric(context.metrics, "scheduler_latency_us"), context.path.scheduler.latency_us)

    def loss_ppm(self, context: CompiledPathContext) -> int:
        """Return smoothed loss in parts per million."""
        return _first_int(_metric(context.metrics, "loss_ppm"), context.path.scheduler.loss_ppm) or 0


class RoundRobinPolicy(SchedulerPolicyBase):
    """Fixed path-order policy for the deterministic baseline behavior."""

    rust_mode: SchedulerMode = "round_robin"


class WeightedRoundRobinPolicy(SchedulerPolicyBase):
    """Weighted path-order policy compiled directly from Python path weights."""

    rust_mode: SchedulerMode = "weighted_round_robin"


class SrttPolicy(SchedulerPolicyBase):
    """MPTCP-style smoothed RTT first policy, using Gatherlink's latency primitive."""

    rust_mode: SchedulerMode = "lowest_latency"


class LossAwarePolicy(SchedulerPolicyBase):
    """Prefer paths with lower loss before latency or capacity tie-breakers."""

    rust_mode: SchedulerMode = "loss_aware"


class CapacityAwarePolicy(SchedulerPolicyBase):
    """Prefer paths with larger local TX capacity."""

    rust_mode: SchedulerMode = "capacity_aware"

    def weight(self, context: CompiledPathContext) -> int:
        return capacity_weight(self.tx_capacity_bps(context))


class LeastQueuePolicy(SchedulerPolicyBase):
    """Prefer paths with lower compiled queue pressure once live queue metrics exist."""

    rust_mode: SchedulerMode = "least_queue"


class EarliestCompletionFirstPolicy(SchedulerPolicyBase):
    """MPTCP ECF-inspired policy using latency plus estimated packet transmit time."""

    rust_mode: SchedulerMode = "earliest_completion_first"


class BlockingEstimationPolicy(SchedulerPolicyBase):
    """BLEST-inspired policy that avoids paths likely to create reorder blocking."""

    rust_mode: SchedulerMode = "blocking_estimation"


class BalancedPolicy(SchedulerPolicyBase):
    """Hybrid default candidate: capacity, latency, loss, and queue facts all matter."""

    rust_mode: SchedulerMode = "balanced"

    def weight(self, context: CompiledPathContext) -> int:
        return adaptive_weight(
            tx_capacity_bps=self.tx_capacity_bps(context),
            latency_us=self.latency_us(context),
            loss_ppm=self.loss_ppm(context),
            queue_depth_packets=context.metrics.queue_depth_packets if context.metrics else 0,
            queue_depth_bytes=context.metrics.queue_depth_bytes if context.metrics else 0,
        )


class AdaptivePolicy(BalancedPolicy):
    """Continuously recompiled policy for production telemetry-driven scheduling."""

    rust_mode: SchedulerMode = "adaptive"


POLICIES: dict[SchedulerPolicy, type[SchedulerPolicyBase]] = {
    "round_robin": RoundRobinPolicy,
    "weighted_round_robin": WeightedRoundRobinPolicy,
    "srtt": SrttPolicy,
    "lowest_latency": SrttPolicy,
    "loss_aware": LossAwarePolicy,
    "capacity_aware": CapacityAwarePolicy,
    "least_queue": LeastQueuePolicy,
    "earliest_completion_first": EarliestCompletionFirstPolicy,
    "blocking_estimation": BlockingEstimationPolicy,
    "balanced": BalancedPolicy,
    "adaptive": AdaptivePolicy,
}


def compile_path_policy(
    path: PathConfig,
    *,
    index: int,
    mode: SchedulerPolicy,
    telemetry: SchedulerTelemetrySnapshot | None = None,
) -> RuntimePathSchedulerConfig:
    """Compile one configured path and optional telemetry into Rust primitives."""
    metrics = telemetry.paths.get(path.name) if telemetry else None
    return policy_for_mode(mode).compile_path(CompiledPathContext(path=path, index=index, metrics=metrics))


def rust_mode_for_policy(mode: SchedulerPolicy) -> SchedulerMode:
    """Return the Rust execution target for a Python scheduler policy."""
    return policy_for_mode(mode).rust_mode


def policy_for_mode(mode: SchedulerPolicy) -> SchedulerPolicyBase:
    """Instantiate the policy implementation for a user-facing policy name."""
    return POLICIES[mode]()


def _metric(metrics: PathSchedulerMetrics | None, field_name: str) -> int | None:
    """Read a metric field without making policy code care how it is stored."""
    if metrics is None:
        return None
    value = getattr(metrics, field_name)
    return int(value) if value is not None else None


def _first_int(*values: int | None) -> int | None:
    """Return the first known integer from telemetry and static config."""
    for value in values:
        if value is not None:
            return int(value)
    return None
