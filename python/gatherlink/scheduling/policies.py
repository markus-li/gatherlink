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
ORDERED_REORDER_HOLD_MIN_US = 2_000
ORDERED_REORDER_HOLD_MAX_US = 150_000
ORDERED_REORDER_HOLD_MARGIN_NUMERATOR = 5
ORDERED_REORDER_HOLD_MARGIN_DENOMINATOR = 4
ORDERED_REORDER_HOLD_JITTER_MULTIPLIER = 2
ORDERED_IN_FLIGHT_BDP_MULTIPLIER = 2
ORDERED_MIN_IN_FLIGHT_PACKETS = 4
ORDERED_MAX_IN_FLIGHT_PACKETS = 8192
ORDERED_MIN_IN_FLIGHT_BYTES = 16 * 1024
ORDERED_MAX_IN_FLIGHT_BYTES = 16 * 1024 * 1024
ORDERED_CAPACITY_AWARE_REORDER_HOLD_MIN_US = 25_000
ORDERED_CAPACITY_AWARE_LARGE_MTU_BYTES = 4096
ORDERED_MAX_PRESSURE_CREDIT_DIVISOR = 16
ORDERED_REORDER_PRESSURE_PACKET_STEP = 1024
ORDERED_REORDER_BUFFER_AGE_STEP_US = 25_000
ORDERED_QUEUE_PRESSURE_PACKET_STEP = 128
ORDERED_IN_FLIGHT_PRESSURE_PACKET_STEP = 512
ORDERED_IN_FLIGHT_PRESSURE_BYTE_STEP = 512 * 1024
ORDERED_PREDICTED_DELIVERY_STEP_US = 25_000
ORDERED_LOSS_PRESSURE_PPM_STEP = 10_000
ORDERED_DROP_PRESSURE_PACKET_STEP = 4096
ORDERED_SEND_FAILURE_PACKET_STEP = 16
ORDERED_JITTER_PRESSURE_STEP_US = 10_000
ORDERED_REORDER_RATIO_PRESSURE_STEP_PPM = 10_000
ORDERED_DROP_RATIO_PRESSURE_STEP_PPM = 5_000
FLOWLET_ADAPTIVE_DEFAULT_IDLE_US = 25_000
FLOWLET_ADAPTIVE_DEFAULT_MAX_HOLD_US = 100_000
FLOWLET_ADAPTIVE_DEFAULT_PATH_RUN_DATAGRAMS = 4
REAL_DATA_LATENCY_ORDERED_CREDIT_MULTIPLIER = 2
SCHEDULER_LATENCY_MAX_US = 10_000_000


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
            enabled=path.scheduler.enabled and context.state != "disabled",
            state=context.state,
            weight=self.weight(context),
            mtu=path.scheduler.mtu,
            tx_capacity_bps=self.tx_capacity_bps(context),
            rx_capacity_bps=self.rx_capacity_bps(context),
            latency_us=self.latency_us(context),
            loss_ppm=self.loss_ppm(context),
            reorder_hold_us=self.reorder_hold_us(context),
            max_in_flight_packets=self.max_in_flight_packets(context),
            max_in_flight_bytes=self.max_in_flight_bytes(context),
            pacing_budget_bps=self.pacing_budget_bps(context),
            queue_depth_packets=context.metrics.queue_depth_packets if context.metrics else 0,
            queue_depth_bytes=context.metrics.queue_depth_bytes if context.metrics else 0,
            queue_oldest_age_us=context.metrics.queue_oldest_age_us if context.metrics else 0,
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
        return _bounded_scheduler_latency_us(
            _first_int(_metric(context.metrics, "scheduler_latency_us"), context.path.scheduler.latency_us)
        )

    def loss_ppm(self, context: CompiledPathContext) -> int:
        """Return smoothed loss in parts per million."""
        return _first_int(_metric(context.metrics, "loss_ppm"), context.path.scheduler.loss_ppm) or 0

    def reorder_hold_us(self, context: CompiledPathContext) -> int:
        """Return the receive-side reorder budget Rust should observe."""
        return context.path.scheduler.reorder_hold_us

    def max_in_flight_packets(self, context: CompiledPathContext) -> int:
        """Return the per-path packet credit compiled for Rust."""
        return context.path.scheduler.max_in_flight_packets or DEFAULT_MAX_IN_FLIGHT_PACKETS

    def max_in_flight_bytes(self, context: CompiledPathContext) -> int:
        """Return the per-path byte credit compiled for Rust."""
        return context.path.scheduler.max_in_flight_bytes or DEFAULT_MAX_IN_FLIGHT_BYTES

    def pacing_budget_bps(self, context: CompiledPathContext) -> int:
        """Return the per-path pacing budget Rust should enforce, or zero to bypass."""
        return context.path.scheduler.pacing_budget_bps


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
    """Split traffic by larger local TX capacity using compiled Rust weights."""

    rust_mode: SchedulerMode = "weighted_round_robin"

    def weight(self, context: CompiledPathContext) -> int:
        return capacity_weight(self.tx_capacity_bps(context))


class LeastQueuePolicy(SchedulerPolicyBase):
    """Prefer paths with lower queue pressure reported by Rust or carrier status."""

    rust_mode: SchedulerMode = "least_queue"


class EarliestCompletionFirstPolicy(SchedulerPolicyBase):
    """MPTCP ECF-inspired policy using latency plus estimated packet transmit time."""

    rust_mode: SchedulerMode = "earliest_completion_first"


class BlockingEstimationPolicy(SchedulerPolicyBase):
    """BLEST-inspired policy that avoids paths likely to create reorder blocking."""

    rust_mode: SchedulerMode = "blocking_estimation"


class OrderedMultipathPolicy(SchedulerPolicyBase):
    """
    MPTCP-inspired ordered service-flow policy.

    Python owns when this policy is appropriate and how path telemetry is
    smoothed. Rust receives the same primitive path facts as other modes, then
    uses a tiny virtual send timeline to avoid blind per-packet striping.
    """

    rust_mode: SchedulerMode = "ordered_multipath"

    def latency_us(self, context: CompiledPathContext) -> int | None:
        """Keep Rust's ordered timeline consistent with Python's credit model."""
        configured_latency_us = _bounded_scheduler_latency_us(_ordered_latency_us(context.metrics))
        if configured_latency_us is None:
            configured_latency_us = _bounded_scheduler_latency_us(context.path.scheduler.latency_us)
        if configured_latency_us is None and _ordered_latency_may_use_generic(context.metrics):
            configured_latency_us = super().latency_us(context)
        return configured_latency_us or _ordered_reorder_hold_us(
            configured_latency_us,
            context.path.scheduler.reorder_hold_us,
            _path_jitter_us(context.metrics),
        )

    def reorder_hold_us(self, context: CompiledPathContext) -> int:
        """Bound single-flow reordering unless config explicitly overrides it."""
        return _ordered_reorder_hold_us(
            _bounded_scheduler_latency_us(_ordered_latency_us(context.metrics)) or super().latency_us(context),
            context.path.scheduler.reorder_hold_us,
            _path_jitter_us(context.metrics),
        )

    def max_in_flight_packets(self, context: CompiledPathContext) -> int:
        """Compile a path credit from the local bandwidth-delay product."""
        if context.path.scheduler.max_in_flight_packets:
            return context.path.scheduler.max_in_flight_packets
        bytes_credit = self.max_in_flight_bytes(context)
        mtu = max(1, context.path.scheduler.mtu)
        packets = max(ORDERED_MIN_IN_FLIGHT_PACKETS, bytes_credit // mtu)
        return min(ORDERED_MAX_IN_FLIGHT_PACKETS, packets)

    def max_in_flight_bytes(self, context: CompiledPathContext) -> int:
        """Compile a byte credit from capacity and latency facts."""
        if context.path.scheduler.max_in_flight_bytes:
            return context.path.scheduler.max_in_flight_bytes
        capacity_bps = _ordered_credit_capacity_bps(self.tx_capacity_bps(context), self.rx_capacity_bps(context))
        latency_us = self.latency_us(context) or self.reorder_hold_us(context)
        if capacity_bps is None or latency_us is None:
            return ORDERED_MIN_IN_FLIGHT_BYTES
        bdp_bytes = capacity_bps * latency_us // 8_000_000
        credit = bdp_bytes * ORDERED_IN_FLIGHT_BDP_MULTIPLIER
        credit //= _ordered_pressure_credit_divisor(context.metrics)
        if _trusted_real_data_latency(context.metrics):
            # Matched real payload tx/rx timing is the best signal we have for
            # ordered TCP-like traffic. It earns a conservative credit release
            # because arrival predictions are less speculative than RTT/2 or
            # peer-advertised latency.
            credit *= REAL_DATA_LATENCY_ORDERED_CREDIT_MULTIPLIER
        return max(ORDERED_MIN_IN_FLIGHT_BYTES, min(ORDERED_MAX_IN_FLIGHT_BYTES, credit))

    def pacing_budget_bps(self, context: CompiledPathContext) -> int:
        """
        Compile a pacing budget for ordered pressure modes.

        Zero means fully bypassed in Rust. Ordered policies already use a
        virtual timeline to spread packets by predicted arrival, and VM TCP
        benchmarks showed packet-thread sleeping can become the bottleneck on
        clean high-speed links. Keep explicit operator pacing available, but do
        not turn it on implicitly from capacity hints.
        """
        if context.path.scheduler.pacing_budget_bps:
            return context.path.scheduler.pacing_budget_bps
        return 0


class OrderedMultipathCapacityAwarePolicy(OrderedMultipathPolicy):
    """
    Ordered multipath variant with capacity-derived path shares.

    The original `ordered_multipath` policy keeps the MPTCP-inspired virtual
    timeline isolated for A/B tests. This variant gives that same Rust primitive
    capacity-aware weights and pressure feedback so WAN benchmark changes can be
    compared without changing the baseline ordered mode.
    """

    def weight(self, context: CompiledPathContext) -> int:
        """Compile the ordered path share from capacity and live pressure facts."""
        base_weight = CapacityAwarePolicy().weight(context)
        pressure_divisor = _ordered_pressure_credit_divisor(context.metrics)
        return max(1, base_weight // pressure_divisor)

    def reorder_hold_us(self, context: CompiledPathContext) -> int:
        """
        Give clean capacity-split ordered mode enough receive slack.

        Low-latency lab paths often do not carry measured latency yet. The base
        ordered policy therefore uses the small global minimum, which is safe
        but makes large-MTU clean capacity profiles underuse faster paths. This
        variant is explicitly the capacity-aware ordered policy, so Python can
        compile a larger reorder budget while Rust still only executes a number.
        """
        configured_hold_us = context.path.scheduler.reorder_hold_us
        if configured_hold_us:
            return configured_hold_us
        base_hold_us = _ordered_reorder_hold_us(
            super().latency_us(context),
            configured_hold_us,
            _path_jitter_us(context.metrics),
        )
        if context.path.scheduler.mtu >= ORDERED_CAPACITY_AWARE_LARGE_MTU_BYTES:
            return max(ORDERED_CAPACITY_AWARE_REORDER_HOLD_MIN_US, base_hold_us)
        return base_hold_us


class SingleBestPathPolicy(CapacityAwarePolicy):
    """
    TCP-friendly policy compiled by Python into one active best path.

    This is not Rust policy. It gives the coordinator a conservative building
    block for opaque order-sensitive tunnels such as WireGuard carrying TCP:
    Python chooses the stable path set, then Rust executes ordinary weighted
    primitives with all non-selected paths in drain/probe state.
    """


class ArrivalGuardedCapacityPolicy(CapacityAwarePolicy):
    """
    Capacity split with Python-side predicted-arrival demotion.

    This policy deliberately compiles to weighted Rust primitives. Python
    estimates whether a path's latency, queue, and capacity facts would arrive
    outside the receiver's reorder budget, then demotes only those paths before
    Rust sees the runtime state.
    """


class FlowletAdaptivePolicy(SchedulerPolicyBase):
    """
    WireGuard-friendly adaptive policy name.

    Python exposes this as a first-class policy so operators and tests can ask
    for flowlet behavior explicitly. Rust still receives ordinary adaptive path
    primitives plus the per-service flowlet stickiness values compiled during
    runtime expansion.
    """

    rust_mode: SchedulerMode = "adaptive"

    def weight(self, context: CompiledPathContext) -> int:
        """Compile the same hybrid weight used by adaptive policy."""
        return adaptive_weight(
            tx_capacity_bps=self.tx_capacity_bps(context),
            latency_us=self.latency_us(context),
            loss_ppm=self.loss_ppm(context),
            queue_depth_packets=context.metrics.queue_depth_packets if context.metrics else 0,
            queue_depth_bytes=context.metrics.queue_depth_bytes if context.metrics else 0,
            jitter_us=_path_jitter_us(context.metrics),
        )


class LatencyGuardedCapacityPolicy(CapacityAwarePolicy):
    """
    Capacity policy guarded by latency/jitter facts.

    Python drains clear latency outliers before compiling the remaining paths
    to capacity weights. Rust still receives ordinary weighted execution
    primitives; the latency guard and capacity meaning stay in Python.
    """


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
            jitter_us=_path_jitter_us(context.metrics),
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
    "ordered_multipath": OrderedMultipathPolicy,
    "ordered_multipath_capacity_aware": OrderedMultipathCapacityAwarePolicy,
    "single_best_path": SingleBestPathPolicy,
    "arrival_guarded_capacity": ArrivalGuardedCapacityPolicy,
    "flowlet_adaptive": FlowletAdaptivePolicy,
    "latency_guarded_capacity": LatencyGuardedCapacityPolicy,
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


def _bounded_scheduler_latency_us(value: int | None) -> int | None:
    """
    Keep remote telemetry from escaping Python's scheduler boundary.

    Long-running lossy tests can briefly expose unmatched process-clock samples
    before the control clock has enough proof. Rust only needs a compact path
    latency primitive, so unreasonable values are ignored instead of being
    allowed to kill a hot reapply.
    """
    if value is None:
        return None
    if value < 0 or value > SCHEDULER_LATENCY_MAX_US:
        logger.warning("scheduler ignored unreasonable latency value", extra={"latency_us": value})
        return None
    return value


def _ordered_reorder_hold_us(
    latency_us: int | None,
    configured_reorder_hold_us: int,
    jitter_us: int | None = None,
) -> int:
    """Return the reorder hold Python compiles for ordered multipath."""
    if configured_reorder_hold_us:
        return configured_reorder_hold_us
    if latency_us is None:
        latency_budget_us = ORDERED_REORDER_HOLD_MIN_US
    else:
        latency_budget_us = (
            latency_us * ORDERED_REORDER_HOLD_MARGIN_NUMERATOR // ORDERED_REORDER_HOLD_MARGIN_DENOMINATOR
        )
    if jitter_us is not None:
        latency_budget_us += jitter_us * ORDERED_REORDER_HOLD_JITTER_MULTIPLIER
    return max(ORDERED_REORDER_HOLD_MIN_US, min(ORDERED_REORDER_HOLD_MAX_US, latency_budget_us))


def _ordered_pressure_credit_divisor(metrics: PathSchedulerMetrics | None) -> int:
    """
    Convert receiver feedback into a bounded sender-credit reduction.

    This is the first explicit MPTCP-style feedback loop in Python policy: peer
    loss, queue, and reorder pressure reduce how much ordered multipath work
    Rust may keep in flight on that path. Rust still sees only the compiled
    credit primitive.
    """
    if metrics is None:
        return 1
    score = 0
    score += min(6, metrics.local_drops // ORDERED_DROP_PRESSURE_PACKET_STEP)
    score += min(4, metrics.send_failures // ORDERED_SEND_FAILURE_PACKET_STEP)
    score += min(4, metrics.receive_gaps // ORDERED_REORDER_PRESSURE_PACKET_STEP)
    score += min(4, metrics.reorder_depth_packets // ORDERED_REORDER_PRESSURE_PACKET_STEP)
    score += min(4, metrics.reorder_buffer_packets // ORDERED_REORDER_PRESSURE_PACKET_STEP)
    score += min(4, metrics.reorder_buffer_oldest_age_us // ORDERED_REORDER_BUFFER_AGE_STEP_US)
    score += min(4, metrics.queue_depth_packets // ORDERED_QUEUE_PRESSURE_PACKET_STEP)
    score += min(4, metrics.scheduler_in_flight_packets // ORDERED_IN_FLIGHT_PRESSURE_PACKET_STEP)
    score += min(4, metrics.scheduler_in_flight_bytes // ORDERED_IN_FLIGHT_PRESSURE_BYTE_STEP)
    score += min(4, metrics.scheduler_predicted_delivery_us // ORDERED_PREDICTED_DELIVERY_STEP_US)
    score += min(4, metrics.loss_ppm // ORDERED_LOSS_PRESSURE_PPM_STEP)
    if metrics.observed_packets > 0:
        receive_gap_ppm = metrics.receive_gaps * 1_000_000 // metrics.observed_packets
        local_drop_ppm = metrics.local_drops * 1_000_000 // metrics.observed_packets
        score += min(6, receive_gap_ppm // ORDERED_REORDER_RATIO_PRESSURE_STEP_PPM)
        score += min(6, local_drop_ppm // ORDERED_DROP_RATIO_PRESSURE_STEP_PPM)
    jitter_us = _path_jitter_us(metrics)
    if jitter_us is not None:
        score += min(4, jitter_us // ORDERED_JITTER_PRESSURE_STEP_US)
    return max(1, min(ORDERED_MAX_PRESSURE_CREDIT_DIVISOR, 1 + score))


def _ordered_credit_capacity_bps(tx_capacity_bps: int | None, rx_capacity_bps: int | None) -> int | None:
    """
    Return the capacity used for ordered in-flight credit.

    For ordered single-flow work, an optimistic local TX estimate is not enough:
    the receiver may be telling us that its observed direction is narrower. Use
    the narrower known directional capacity so Python's compiled credits do not
    let Rust build more in-flight work than the receiver feedback can justify.
    """
    known = [value for value in (tx_capacity_bps, rx_capacity_bps) if value is not None and value > 0]
    return min(known) if known else None


def _path_jitter_us(metrics: PathSchedulerMetrics | None) -> int | None:
    """Return the largest directional jitter fact visible for one path."""
    if metrics is None:
        return None
    values = [value for value in (metrics.tx_jitter_us, metrics.rx_jitter_us) if value is not None]
    return max(values) if values else None


def _ordered_latency_us(metrics: PathSchedulerMetrics | None) -> int | None:
    """
    Return the conservative latency estimate used for ordered service flows.

    Ordered TCP-like tunnels are hurt more by optimistic timing than by modest
    underuse. Prefer the largest directional mean/current value that Python has
    accepted, so Rust's virtual arrival timeline does not accidentally overfill
    a path whose return direction or peer-advertised side is slower.
    """
    if metrics is None:
        return None
    if not _ordered_latency_may_use_generic(metrics):
        return None
    values = [
        value
        for value in (
            metrics.tx_latency_mean_us,
            metrics.tx_latency_current_us,
            metrics.tx_p95_us,
            metrics.rx_latency_mean_us,
            metrics.rx_latency_current_us,
            metrics.rx_p95_us,
        )
        if value is not None
    ]
    return max(values) if values else None


def _ordered_latency_may_use_generic(metrics: PathSchedulerMetrics | None) -> bool:
    """
    Return whether ordered mode should trust latency enough to rank paths.

    Clock-synced one-way samples are useful after a path has carried real
    packets, but startup probes can be directionally noisy. If an ordered TCP
    path has no observed payload yet, leave latency unknown so the path can
    prove itself with bounded credit instead of being drained by a speculative
    arrival estimate.
    """
    if metrics is None:
        return True
    if metrics.has_trusted_real_data_latency:
        return True
    if metrics.latency_source != "clock-synced-one-way":
        return True
    return metrics.observed_packets > 0


def _trusted_real_data_latency(metrics: PathSchedulerMetrics | None) -> bool:
    """Return whether Python should treat latency as real-payload measured."""
    return bool(metrics and metrics.has_trusted_real_data_latency)
