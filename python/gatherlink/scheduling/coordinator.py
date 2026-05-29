"""Python-owned coordinator for switching between compiled scheduler policies."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from gatherlink.config.models import GatherlinkConfig, SchedulerPolicy
from gatherlink.scheduling.metrics import PathSchedulerMetrics, SchedulerTelemetrySnapshot
from gatherlink.scheduling.scoring import CAPACITY_CONFIDENCE_HIGH_PPM, capacity_confidence_ppm
from gatherlink.scheduling.service_intent import ServiceTrafficSummary, service_traffic_summary

COORDINATED_DEFAULT_MIN_DWELL_PACKETS = 50_000
COORDINATED_DEFAULT_MIN_DWELL_SECONDS = 5.0
COORDINATED_DEFAULT_REQUIRED_CONFIDENCE_WINDOWS = 2
COORDINATED_MAX_DECISION_HISTORY = 16
COORDINATED_MAX_RESPONSIVENESS_HISTORY = 8
COORDINATED_STALE_CONTROL_US = 120_000_000
COORDINATED_HIGH_REORDER_PACKETS = 1024
COORDINATED_HIGH_LOSS_PPM = 80_000
COORDINATED_QUEUE_PRESSURE_PACKETS = 512
COORDINATED_LATENCY_GUARD_MIN_SPREAD_US = 20_000
COORDINATED_LATENCY_GUARD_SPREAD_MULTIPLIER = 2
COORDINATED_CAPACITY_SKEW_NUMERATOR = 3
COORDINATED_CAPACITY_SKEW_DENOMINATOR = 2
COORDINATED_HIGH_JITTER_US = 20_000
COORDINATED_TCP_ORDERED_MAX_LOSS_PPM = 10_000
COORDINATED_TCP_ORDERED_MAX_JITTER_US = 12_000
COORDINATED_TCP_ORDERED_MAX_PROVEN_JITTER_US = 75_000
COORDINATED_TCP_ORDERED_MAX_PROVEN_SPREAD_US = 100_000
COORDINATED_TCP_ORDERED_MAX_CLOCK_ERROR_US = 75_000
COORDINATED_TCP_ORDERED_MAX_DATA_LATENCY_US = 500_000
COORDINATED_TCP_ORDERED_MAX_DATA_LATENCY_SPREAD_US = 250_000
COORDINATED_TCP_ORDERED_MIN_CAPACITY_RATIO_NUMERATOR = 1
COORDINATED_TCP_ORDERED_MIN_CAPACITY_RATIO_DENOMINATOR = 3
COORDINATED_TCP_ORDERED_PRESSURE_ESCAPE_MIN_CAPACITY_RATIO_NUMERATOR = 1
COORDINATED_TCP_ORDERED_PRESSURE_ESCAPE_MIN_CAPACITY_RATIO_DENOMINATOR = 8
COORDINATED_TCP_ORDERED_PRESSURE_ESCAPE_PREDICTED_DELIVERY_US = 500_000
COORDINATED_TCP_ORDERED_MAX_QUEUE_PACKETS = 64
COORDINATED_TCP_ORDERED_MAX_IN_FLIGHT_PACKETS = 4_096
COORDINATED_TCP_ORDERED_MAX_IN_FLIGHT_BYTES = 4 * 1024 * 1024
COORDINATED_TCP_ORDERED_MAX_PREDICTED_DELIVERY_US = 150_000
COORDINATED_TCP_ORDERED_MAX_REORDER_BUFFER_PACKETS = 2_048
COORDINATED_TCP_ORDERED_MAX_REORDER_PACKETS = 128
COORDINATED_TCP_ORDERED_MAX_REORDER_PPM = 5_000
COORDINATED_TCP_ORDERED_MIN_KNOWN_PATHS = 2
COORDINATED_TCP_ORDERED_MIN_OBSERVED_PACKETS = 1_000
COORDINATED_TCP_ORDERED_MIN_CONTROL_PROBE_FRAMES = 20
RESPONSIVENESS_WIRED_MAX_LATENCY_US = 25_000
RESPONSIVENESS_WIRED_MAX_JITTER_US = 3_000
RESPONSIVENESS_CELLULAR_MIN_LATENCY_US = 35_000
RESPONSIVENESS_CELLULAR_MIN_JITTER_US = 8_000
RESPONSIVENESS_STARLINK_MIN_LATENCY_US = 45_000
RESPONSIVENESS_STARLINK_MIN_JITTER_US = 15_000
RESPONSIVENESS_VOLATILE_MIN_JITTER_US = 40_000
RESPONSIVENESS_VOLATILE_MIN_LOSS_PPM = 50_000

PathResponsivenessClass = Literal[
    "wired_stable",
    "cellular_like",
    "starlink_like",
    "volatile_wireless",
    "unknown",
]
PATH_RESPONSIVENESS_CLASSES: tuple[PathResponsivenessClass, ...] = (
    "wired_stable",
    "cellular_like",
    "starlink_like",
    "volatile_wireless",
    "unknown",
)

COORDINATED_ALLOWED_POLICIES: set[SchedulerPolicy] = {
    "capacity_aware",
    "arrival_guarded_capacity",
    "latency_guarded_capacity",
    "ordered_multipath_capacity_aware",
    "flowlet_adaptive",
    "single_best_path",
}


@dataclass(frozen=True)
class SchedulerCoordinatorDecision:
    """One explainable coordinator decision."""

    configured_mode: SchedulerPolicy
    effective_mode: SchedulerPolicy
    candidate_mode: SchedulerPolicy
    switched: bool
    reason: str
    confidence: int
    required_confidence: int
    packets_since_switch: int
    minimum_dwell_packets: int
    seconds_since_switch: float
    minimum_dwell_seconds: float
    decision_number: int
    blocked_by: str | None = None
    signals: tuple[str, ...] = ()
    path_profiles: tuple[PathResponsivenessProfile, ...] = ()

    def export_dict(self) -> dict[str, object]:
        """Return a stable diagnostic payload."""
        return {
            "configured_mode": self.configured_mode,
            "effective_mode": self.effective_mode,
            "candidate_mode": self.candidate_mode,
            "switched": self.switched,
            "reason": self.reason,
            "confidence": self.confidence,
            "required_confidence": self.required_confidence,
            "packets_since_switch": self.packets_since_switch,
            "minimum_dwell_packets": self.minimum_dwell_packets,
            "seconds_since_switch": round(self.seconds_since_switch, 3),
            "minimum_dwell_seconds": self.minimum_dwell_seconds,
            "decision_number": self.decision_number,
            "blocked_by": self.blocked_by,
            "signals": list(self.signals),
            "path_profiles": [profile.export_dict() for profile in self.path_profiles],
        }


@dataclass(frozen=True)
class PathResponsivenessProfile:
    """Confidence-scored responsiveness class for one path."""

    path: str
    current_class: PathResponsivenessClass
    stable_class: PathResponsivenessClass
    confidence_ppm: int
    windows: int

    def export_dict(self) -> dict[str, object]:
        """Return a compact diagnostics payload."""
        return {
            "path": self.path,
            "current_class": self.current_class,
            "stable_class": self.stable_class,
            "confidence_ppm": self.confidence_ppm,
            "windows": self.windows,
        }


@dataclass
class SchedulerPolicyCoordinator:
    """
    Choose the effective scheduler policy for `coordinated_adaptive`.

    The coordinator is deliberately a small state machine. Python interprets
    metrics, applies dwell/confidence rules, and returns a normal scheduler
    policy. Rust only sees the resulting compiled primitive mode.
    """

    minimum_dwell_packets: int = COORDINATED_DEFAULT_MIN_DWELL_PACKETS
    minimum_dwell_seconds: float = COORDINATED_DEFAULT_MIN_DWELL_SECONDS
    required_confidence_windows: int = COORDINATED_DEFAULT_REQUIRED_CONFIDENCE_WINDOWS
    now: Callable[[], float] = monotonic
    current_mode: SchedulerPolicy | None = None
    _candidate_mode: SchedulerPolicy | None = None
    _candidate_confidence: int = 0
    _packets_since_switch: int = 0
    _last_switch_at: float = field(default_factory=monotonic)
    _decision_number: int = 0
    _recent_decisions: deque[SchedulerCoordinatorDecision] = field(
        default_factory=lambda: deque(maxlen=COORDINATED_MAX_DECISION_HISTORY),
        init=False,
    )
    _responsiveness_history: dict[str, deque[PathResponsivenessClass]] = field(default_factory=dict, init=False)
    last_decision: SchedulerCoordinatorDecision | None = field(default=None, init=False)

    def choose_effective_mode(
        self,
        config: GatherlinkConfig,
        telemetry: SchedulerTelemetrySnapshot,
        service_traffic: ServiceTrafficSummary | None = None,
    ) -> SchedulerPolicy:
        """Return the scheduler policy to compile for this telemetry window."""
        if config.scheduler.mode != "coordinated_adaptive":
            self.current_mode = config.scheduler.mode
            self._candidate_mode = config.scheduler.mode
            self._candidate_confidence = self.required_confidence_windows
            self._packets_since_switch = 0
            self._last_switch_at = self.now()
            self._decision_number += 1
            decision = SchedulerCoordinatorDecision(
                configured_mode=config.scheduler.mode,
                effective_mode=config.scheduler.mode,
                candidate_mode=config.scheduler.mode,
                switched=False,
                reason="configured scheduler is not coordinated_adaptive",
                confidence=self._candidate_confidence,
                required_confidence=self.required_confidence_windows,
                packets_since_switch=self._packets_since_switch,
                minimum_dwell_packets=self.minimum_dwell_packets,
                seconds_since_switch=0.0,
                minimum_dwell_seconds=self.minimum_dwell_seconds,
                decision_number=self._decision_number,
            )
            self._record_decision(decision)
            return config.scheduler.mode

        if self.current_mode not in COORDINATED_ALLOWED_POLICIES:
            self.current_mode = known_good_fallback_policy(config, telemetry)
            self._last_switch_at = self.now()

        self._packets_since_switch += _observed_packets(telemetry)
        self._decision_number += 1
        candidate, reason, signals = choose_candidate_policy(
            config,
            telemetry,
            fallback=self.current_mode,
            service_traffic=service_traffic,
        )
        path_profiles = self._update_path_profiles(telemetry)
        signals = tuple(dict.fromkeys((*signals, *self._profile_signals(path_profiles))))
        if candidate == self._candidate_mode:
            self._candidate_confidence += 1
        else:
            self._candidate_mode = candidate
            self._candidate_confidence = 1

        blocked_by = self._switch_blocker(candidate)
        switched = False
        if blocked_by is None and candidate != self.current_mode:
            self.current_mode = candidate
            self._packets_since_switch = 0
            self._last_switch_at = self.now()
            switched = True

        seconds_since_switch = max(0.0, self.now() - self._last_switch_at)
        decision = SchedulerCoordinatorDecision(
            configured_mode=config.scheduler.mode,
            effective_mode=self.current_mode,
            candidate_mode=candidate,
            switched=switched,
            reason=reason,
            confidence=self._candidate_confidence,
            required_confidence=self.required_confidence_windows,
            packets_since_switch=self._packets_since_switch,
            minimum_dwell_packets=self.minimum_dwell_packets,
            seconds_since_switch=seconds_since_switch,
            minimum_dwell_seconds=self.minimum_dwell_seconds,
            decision_number=self._decision_number,
            blocked_by=blocked_by,
            signals=signals,
            path_profiles=path_profiles,
        )
        self._record_decision(decision)
        return self.current_mode

    def _switch_blocker(self, candidate: SchedulerPolicy) -> str | None:
        """Return the reason a candidate cannot yet replace the current mode."""
        if candidate == self.current_mode:
            return None
        if self._candidate_confidence < self.required_confidence_windows:
            return "waiting_for_confidence"
        if self._packets_since_switch < self.minimum_dwell_packets:
            return "minimum_packet_dwell"
        if self.now() - self._last_switch_at < self.minimum_dwell_seconds:
            return "minimum_time_dwell"
        return None

    def recent_decisions(self) -> list[dict[str, object]]:
        """Return compact bounded coordinator history for operator diagnostics."""
        return [decision.export_dict() for decision in self._recent_decisions]

    def _record_decision(self, decision: SchedulerCoordinatorDecision) -> None:
        """Keep only bounded compact decision state, never per-packet logs."""
        self.last_decision = decision
        self._recent_decisions.append(decision)

    def _update_path_profiles(self, telemetry: SchedulerTelemetrySnapshot) -> tuple[PathResponsivenessProfile, ...]:
        """
        Update bounded path responsiveness history and return scored profiles.

        This is a coordinator diagnostic and policy hint only. It does not
        override explicit config by itself; it gives Python a sustained view so
        a single jitter spike does not relabel a path as volatile forever.
        """
        seen: set[str] = set()
        profiles: list[PathResponsivenessProfile] = []
        for path_name, metric in sorted(telemetry.paths.items()):
            seen.add(path_name)
            current_class = classify_path_responsiveness(metric)
            history = self._responsiveness_history.setdefault(
                path_name,
                deque(maxlen=COORDINATED_MAX_RESPONSIVENESS_HISTORY),
            )
            history.append(current_class)
            stable_class, confidence_ppm = _stable_responsiveness_class(history)
            profiles.append(
                PathResponsivenessProfile(
                    path=path_name,
                    current_class=current_class,
                    stable_class=stable_class,
                    confidence_ppm=confidence_ppm,
                    windows=len(history),
                )
            )
        for stale_path in set(self._responsiveness_history) - seen:
            del self._responsiveness_history[stale_path]
        return tuple(profiles)

    @staticmethod
    def _profile_signals(profiles: tuple[PathResponsivenessProfile, ...]) -> tuple[str, ...]:
        """Return compact stable-profile signals for diagnostics."""
        signals = []
        for profile in profiles:
            if profile.stable_class != "unknown" and profile.confidence_ppm >= 500_000:
                signals.append(f"profile:{profile.stable_class}")
        return tuple(dict.fromkeys(signals))


def choose_candidate_policy(
    config: GatherlinkConfig,
    telemetry: SchedulerTelemetrySnapshot,
    *,
    fallback: SchedulerPolicy | None = None,
    service_traffic: ServiceTrafficSummary | None = None,
) -> tuple[SchedulerPolicy, str, tuple[str, ...]]:
    """Classify the current telemetry window into a concrete scheduler policy."""
    service_traffic = service_traffic or service_traffic_summary(config.services)
    fallback = fallback or known_good_fallback_policy(config, telemetry, service_traffic=service_traffic)
    metrics = list(telemetry.paths.values())
    signals = _telemetry_signals(config, metrics) + service_traffic.signals()
    if not metrics:
        return fallback, "no telemetry; using known-good fallback", signals
    if _telemetry_is_stale(metrics):
        return fallback, "telemetry stale; holding known-good policy", signals
    effective_bias = _effective_traffic_bias(config, service_traffic)
    if effective_bias == "tcp":
        return _choose_tcp_biased_candidate(config, metrics, signals, fallback)
    if effective_bias == "udp":
        return _choose_udp_biased_candidate(config, metrics, signals, fallback)
    if service_traffic.has_mixed_known_classes:
        return _choose_mixed_service_candidate(config, metrics, signals, fallback, service_traffic)
    if _has_high_jitter(metrics) and _has_latency_spread(metrics) and _has_capacity_hints(config, metrics):
        return "flowlet_adaptive", "jitter pressure favors flowlet stickiness", signals
    if _has_latency_spread(metrics) and _has_queue_pressure(metrics) and _has_capacity_hints(config, metrics):
        return "arrival_guarded_capacity", "latency spread with queue pressure favors arrival guard", signals
    if _has_latency_spread(metrics) and (_has_reorder_or_loss(metrics) or _has_queue_pressure(metrics)):
        return "latency_guarded_capacity", "latency spread with reorder/loss or queue pressure", signals
    if _has_queue_pressure(metrics) or _has_high_loss(metrics):
        return "capacity_aware", "path pressure favors capacity-aware split and health guards", signals
    if (
        _has_high_reorder_pressure(metrics)
        and _has_capacity_hints(config, metrics)
        and _capacity_confidence_is_high(metrics)
    ):
        return "ordered_multipath_capacity_aware", "reorder pressure with confident path capacity", signals
    return known_good_fallback_policy(config, telemetry), "stable telemetry; using known-good fallback", signals


def _choose_tcp_biased_candidate(
    config: GatherlinkConfig,
    metrics: list[PathSchedulerMetrics],
    signals: tuple[str, ...],
    fallback: SchedulerPolicy,
) -> tuple[SchedulerPolicy, str, tuple[str, ...]]:
    """
    Choose a conservative TCP-like policy for opaque ordered UDP tunnels.

    This is still Gatherlink scheduling, not packet inspection. Operators use
    `scheduler.traffic_bias=tcp` when the service is expected to carry an
    order-sensitive tunnel such as WireGuard with TCP inside it. Python first
    looks for a low-pressure path set where ordered multipath can safely use
    more than one path. If the evidence is not clean enough, the coordinator
    falls back to stickier/conservative policies instead of gambling with TCP
    ordering.
    """
    if _tcp_ordered_pressure_escape_is_safe(config, metrics):
        return (
            "ordered_multipath_capacity_aware",
            "tcp bias: best path pressure allows bounded ordered capacity escape",
            signals,
        )
    if any(_tcp_ordered_reorder_pressure_is_high(metric) for metric in metrics):
        return "single_best_path", "tcp bias: receiver reorder pressure favors best path", signals
    if _has_capacity_hints(config, metrics) and not _tcp_ordered_capacity_ratio_is_safe(config, metrics):
        return (
            "single_best_path",
            "tcp bias: skewed path capacity favors known-good fallback",
            signals,
        )
    if _tcp_ordered_latency_quality_is_too_noisy(metrics):
        return (
            "single_best_path",
            "tcp bias: latency uncertainty favors best path",
            signals,
        )
    if _tcp_ordered_multipath_is_safe(config, metrics):
        return (
            "ordered_multipath_capacity_aware",
            "tcp bias: clean proven paths allow ordered capacity aggregation",
            signals,
        )
    if _has_high_jitter(metrics) and _has_latency_spread(metrics) and _has_capacity_hints(config, metrics):
        return "single_best_path", "tcp bias: jitter and latency spread favor best path", signals
    if _has_latency_spread(metrics):
        return "single_best_path", "tcp bias: latency spread favors best path", signals
    if _has_queue_pressure(metrics) or _has_high_loss(metrics):
        return "single_best_path", "tcp bias: path pressure favors best path", signals
    if _has_capacity_hints(config, metrics):
        return (
            "single_best_path",
            "tcp bias: stable paths favor best path",
            signals,
        )
    return "single_best_path", "tcp bias: using best path fallback", signals


def _tcp_ordered_multipath_is_safe(config: GatherlinkConfig, metrics: list[PathSchedulerMetrics]) -> bool:
    """
    Return whether TCP-like traffic may try ordered multipath instead of best-path.

    This is deliberately conservative. The goal is not to force aggregation in
    every TCP-shaped run; it is to let Python promote ordered multipath when
    telemetry says there are several known-capacity paths with low loss, bounded
    jitter, little receiver reorder pressure, and no meaningful queue pressure.
    Rust still only executes the compiled ordered timeline and credits.
    """
    if not _has_capacity_hints(config, metrics) or not _capacity_confidence_is_high(metrics):
        return False

    known_paths = [
        metric
        for metric in metrics
        if _metric_or_config_capacity(config, metric) is not None
        and _tcp_latency_us(metric) is not None
        and metric.stale_control_age_us < COORDINATED_STALE_CONTROL_US
    ]
    proven_paths = [
        metric
        for metric in known_paths
        if _tcp_ordered_path_has_enough_probe_activity(metric) and _tcp_ordered_path_has_latency_proof(metric)
    ]
    if len(proven_paths) < COORDINATED_TCP_ORDERED_MIN_KNOWN_PATHS:
        return False
    if not _tcp_ordered_capacity_ratio_is_safe(config, proven_paths):
        return False

    if any(_tcp_ordered_path_has_pressure(metric) for metric in proven_paths):
        return False

    if any(not _tcp_ordered_jitter_is_tolerable(metric) for metric in proven_paths):
        return False

    latencies = [_tcp_latency_us(metric) for metric in proven_paths if _tcp_latency_us(metric) is not None]
    return _tcp_ordered_latency_spread_is_tolerable(proven_paths, latencies)


def _tcp_ordered_reorder_pressure_is_high(metric: PathSchedulerMetrics) -> bool:
    """Return whether receiver reorder facts are too noisy for TCP aggregation."""
    # `receive_gaps` counts packets that needed the reorder buffer, which is
    # normal for ordered multipath. Treat it as harmful only when it comes with
    # unresolved depth or other pressure; otherwise the buffer is doing useful
    # work and should not block promotion by itself.
    reorder_packets = metric.reorder_depth_packets
    if metric.observed_packets <= 0 and metric.receive_gaps > COORDINATED_TCP_ORDERED_MAX_REORDER_PACKETS:
        return True
    if reorder_packets == 0 and (
        metric.loss_ppm > 0
        or metric.local_drops > 0
        or metric.send_failures > 0
        or metric.queue_depth_packets > COORDINATED_TCP_ORDERED_MAX_QUEUE_PACKETS
    ):
        reorder_packets = metric.receive_gaps
    if reorder_packets <= COORDINATED_TCP_ORDERED_MAX_REORDER_PACKETS:
        return False
    if metric.observed_packets <= 0:
        return True
    reorder_ppm = reorder_packets * 1_000_000 // max(1, metric.observed_packets)
    return reorder_ppm > COORDINATED_TCP_ORDERED_MAX_REORDER_PPM


def _tcp_ordered_capacity_ratio_is_safe(config: GatherlinkConfig, metrics: list[PathSchedulerMetrics]) -> bool:
    """
    Return whether candidate TCP paths are close enough in capacity to aggregate.

    Current ordered multipath is useful for similarly capable paths, but a tiny
    slow path can still disturb TCP more than it helps. Keep very asymmetric
    TCP services on the best path until a future scheduler has stronger
    sender-side in-flight and receiver-feedback control.
    """
    capacities = [_metric_or_config_capacity(config, metric) for metric in metrics]
    known = [capacity for capacity in capacities if capacity is not None and capacity > 0]
    if len(known) < COORDINATED_TCP_ORDERED_MIN_KNOWN_PATHS:
        return False
    return min(known) * COORDINATED_TCP_ORDERED_MIN_CAPACITY_RATIO_DENOMINATOR >= (
        max(known) * COORDINATED_TCP_ORDERED_MIN_CAPACITY_RATIO_NUMERATOR
    )


def _tcp_ordered_pressure_escape_is_safe(config: GatherlinkConfig, metrics: list[PathSchedulerMetrics]) -> bool:
    """
    Return whether a saturated best path may try bounded ordered aggregation.

    The normal TCP bias is intentionally conservative and avoids very skewed
    path sets. That breaks down when the best path is clearly overloaded: doing
    nothing means loss or deep queueing. This escape hatch is still Python-owned
    policy and still compiles to ordinary ordered primitives. It only opens when
    the best-capacity path is under visible pressure and at least one alternate
    has enough configured/proven capacity plus control activity to act as a
    relief path.
    """
    if not _has_capacity_hints(config, metrics) or not _capacity_confidence_is_high(metrics):
        return False
    known = [
        (metric, _metric_or_config_capacity(config, metric))
        for metric in metrics
        if _metric_or_config_capacity(config, metric) is not None
        and _tcp_ordered_path_has_latency_proof(metric)
        and metric.stale_control_age_us < COORDINATED_STALE_CONTROL_US
    ]
    known = [(metric, capacity) for metric, capacity in known if capacity is not None and capacity > 0]
    if len(known) < COORDINATED_TCP_ORDERED_MIN_KNOWN_PATHS:
        return False

    best_metric, best_capacity = max(known, key=lambda item: (item[1], -item[0].loss_ppm))
    if not _tcp_ordered_best_path_is_overloaded(best_metric):
        return False

    for metric, capacity in known:
        if metric.path_name == best_metric.path_name:
            continue
        if capacity * COORDINATED_TCP_ORDERED_PRESSURE_ESCAPE_MIN_CAPACITY_RATIO_DENOMINATOR < (
            best_capacity * COORDINATED_TCP_ORDERED_PRESSURE_ESCAPE_MIN_CAPACITY_RATIO_NUMERATOR
        ):
            continue
        if _tcp_ordered_alternate_path_is_clean_enough(metric):
            return True
    return False


def _tcp_ordered_best_path_is_overloaded(metric: PathSchedulerMetrics) -> bool:
    """Return whether the preferred TCP path is visibly over capacity."""
    if metric.local_drops > 0 or metric.send_failures > 0:
        return True
    if metric.loss_ppm > COORDINATED_TCP_ORDERED_MAX_LOSS_PPM:
        return True
    if metric.queue_depth_packets > COORDINATED_TCP_ORDERED_MAX_QUEUE_PACKETS:
        return True
    if metric.scheduler_in_flight_packets > COORDINATED_TCP_ORDERED_MAX_IN_FLIGHT_PACKETS:
        return True
    if metric.scheduler_in_flight_bytes > COORDINATED_TCP_ORDERED_MAX_IN_FLIGHT_BYTES:
        return True
    return metric.scheduler_predicted_delivery_us > COORDINATED_TCP_ORDERED_PRESSURE_ESCAPE_PREDICTED_DELIVERY_US


def _tcp_ordered_alternate_path_is_clean_enough(metric: PathSchedulerMetrics) -> bool:
    """Return whether an alternate path can receive bounded escape traffic."""
    if metric.local_drops > 0 or metric.send_failures > 0:
        return False
    if metric.loss_ppm > COORDINATED_TCP_ORDERED_MAX_LOSS_PPM:
        return False
    if metric.queue_depth_packets > COORDINATED_TCP_ORDERED_MAX_QUEUE_PACKETS:
        return False
    if metric.scheduler_in_flight_packets > COORDINATED_TCP_ORDERED_MAX_IN_FLIGHT_PACKETS:
        return False
    if metric.scheduler_in_flight_bytes > COORDINATED_TCP_ORDERED_MAX_IN_FLIGHT_BYTES:
        return False
    return metric.reorder_buffer_packets <= COORDINATED_TCP_ORDERED_MAX_REORDER_BUFFER_PACKETS


def _tcp_ordered_path_has_pressure(metric: PathSchedulerMetrics) -> bool:
    """Return whether a path has pressure that should block ordered TCP promotion."""
    return (
        metric.loss_ppm > COORDINATED_TCP_ORDERED_MAX_LOSS_PPM
        or metric.local_drops > 0
        or metric.send_failures > 0
        or metric.queue_depth_packets > COORDINATED_TCP_ORDERED_MAX_QUEUE_PACKETS
        or metric.scheduler_in_flight_packets > COORDINATED_TCP_ORDERED_MAX_IN_FLIGHT_PACKETS
        or metric.scheduler_in_flight_bytes > COORDINATED_TCP_ORDERED_MAX_IN_FLIGHT_BYTES
        or metric.scheduler_predicted_delivery_us > COORDINATED_TCP_ORDERED_MAX_PREDICTED_DELIVERY_US
        or metric.reorder_buffer_packets > COORDINATED_TCP_ORDERED_MAX_REORDER_BUFFER_PACKETS
        or _tcp_ordered_reorder_pressure_is_high(metric)
    )


def _tcp_ordered_jitter_is_tolerable(metric: PathSchedulerMetrics) -> bool:
    """
    Return whether jitter still allows TCP-oriented ordered promotion.

    The default threshold stays intentionally sharp, but real asymmetric links
    often report bursty directional jitter while still delivering ordered TCP
    well once Python compiles bounded credits. Only trusted real-payload samples
    with clean pressure counters may use the wider proven-jitter budget.
    """
    jitter_us = _path_jitter_us(metric) or 0
    if jitter_us <= COORDINATED_TCP_ORDERED_MAX_JITTER_US:
        return True
    return (
        metric.has_trusted_real_data_latency
        and not _tcp_ordered_path_has_pressure(metric)
        and jitter_us <= COORDINATED_TCP_ORDERED_MAX_PROVEN_JITTER_US
    )


def _tcp_ordered_latency_spread_is_tolerable(
    proven_paths: list[PathSchedulerMetrics],
    latencies: list[int],
) -> bool:
    """
    Return whether the proven path set can tolerate its latency spread.

    The conservative default remains based on fastest-path latency. A wider
    absolute budget is allowed only when every path has trusted real-payload
    latency and clean pressure counters, which is exactly the condition needed
    before Python can ask Rust to use bounded ordered credits on asymmetric
    links.
    """
    fastest = min(latencies)
    slowest = max(latencies)
    spread = slowest - fastest
    if spread <= max(COORDINATED_LATENCY_GUARD_MIN_SPREAD_US, fastest):
        return True
    return (
        spread <= COORDINATED_TCP_ORDERED_MAX_PROVEN_SPREAD_US
        and all(_tcp_ordered_path_has_latency_proof(metric) for metric in proven_paths)
        and not any(_tcp_ordered_path_has_pressure(metric) for metric in proven_paths)
    )


def _tcp_ordered_latency_quality_is_too_noisy(metrics: list[PathSchedulerMetrics]) -> bool:
    """
    Return whether TCP-like opaque tunnels should avoid capacity striping.

    Ordered multipath can help TCP-shaped tunnels only when Python trusts the
    timing facts enough to compile a bounded delivery timeline. Large clock
    uncertainty or multi-hundred-ms real-payload latency outliers mean the
    safer policy is to stay on the best path until better samples arrive. UDP
    bulk policy does not use this helper, so lossy multipath can still aggregate.
    """
    if any(
        metric.latency_source == "clock-synced-one-way"
        and metric.latency_clock_error_us is not None
        and metric.latency_clock_error_us > COORDINATED_TCP_ORDERED_MAX_CLOCK_ERROR_US
        for metric in metrics
    ):
        return True

    data_latencies = [
        value
        for metric in metrics
        if metric.has_trusted_real_data_latency
        for value in (
            metric.tx_latency_current_us,
            metric.tx_latency_mean_us,
            metric.rx_latency_current_us,
            metric.rx_latency_mean_us,
            metric.tx_p95_us,
            metric.rx_p95_us,
        )
        if value is not None
    ]
    if not data_latencies:
        return False
    if max(data_latencies) > COORDINATED_TCP_ORDERED_MAX_DATA_LATENCY_US:
        return True
    return (
        len(data_latencies) >= 2
        and max(data_latencies) - min(data_latencies) > COORDINATED_TCP_ORDERED_MAX_DATA_LATENCY_SPREAD_US
    )


def _tcp_ordered_path_has_latency_proof(metric: PathSchedulerMetrics) -> bool:
    """
    Return whether latency is strong enough for TCP ordered promotion.

    Matched real-payload one-way samples are best. Clock-synced one-way samples
    are accepted only when the path also has packet observations and a bounded
    clock error; this lets startup use asymmetric paths without claiming that
    the estimate is as strong as data-derived latency.
    """
    if metric.has_trusted_real_data_latency:
        return True
    if (
        metric.latency_source == "clock-synced-one-way"
        and metric.latency_confidence == "good"
        and metric.control_rx_frames >= COORDINATED_TCP_ORDERED_MIN_CONTROL_PROBE_FRAMES
        and metric.control_tx_frames >= COORDINATED_TCP_ORDERED_MIN_CONTROL_PROBE_FRAMES
    ):
        return True
    return (
        metric.latency_source == "clock-synced-one-way"
        and metric.latency_confidence == "good"
        and metric.latency_clock_error_us is not None
        and metric.latency_clock_error_us <= COORDINATED_TCP_ORDERED_MAX_CLOCK_ERROR_US
        and metric.observed_packets >= COORDINATED_TCP_ORDERED_MIN_OBSERVED_PACKETS
    )


def _tcp_ordered_path_has_enough_probe_activity(metric: PathSchedulerMetrics) -> bool:
    """
    Return whether the path has enough packet/control activity for TCP proof.

    A protected service pinned to one path will not create user-service packet
    counters on the other paths. Control metadata is intentionally duplicated
    across paths, so Python can use those existing frames as low-rate probe
    evidence without adding a special Rust behavior or extra user traffic.
    """
    if metric.observed_packets >= COORDINATED_TCP_ORDERED_MIN_OBSERVED_PACKETS:
        return True
    return (
        metric.control_rx_frames >= COORDINATED_TCP_ORDERED_MIN_CONTROL_PROBE_FRAMES
        and metric.control_tx_frames >= COORDINATED_TCP_ORDERED_MIN_CONTROL_PROBE_FRAMES
    )


def _tcp_latency_us(metric: PathSchedulerMetrics) -> int | None:
    """
    Return the conservative latency value used for TCP promotion decisions.

    The generic scheduler latency can use the first known direction because
    many UDP policies need a cheap path rank. TCP-like opaque tunnels need
    stronger evidence: use the largest known directional value so a slow ACK or
    return direction does not make an ordered path look safer than it is.
    """
    values = [
        value
        for value in (
            metric.tx_latency_mean_us,
            metric.tx_latency_current_us,
            metric.rx_latency_mean_us,
            metric.rx_latency_current_us,
        )
        if value is not None
    ]
    return max(values) if values else None


def _choose_udp_biased_candidate(
    config: GatherlinkConfig,
    metrics: list[PathSchedulerMetrics],
    signals: tuple[str, ...],
    fallback: SchedulerPolicy,
) -> tuple[SchedulerPolicy, str, tuple[str, ...]]:
    """Choose an aggregation-first policy for ordinary UDP-like service traffic."""
    if _has_queue_pressure(metrics) or _has_high_loss(metrics):
        return "capacity_aware", "udp bias: pressure favors capacity-aware aggregation", signals
    if _has_capacity_hints(config, metrics):
        return "capacity_aware", "udp bias: capacity hints favor aggregation", signals
    return fallback, "udp bias: using known-good fallback", signals


def known_good_fallback_policy(
    config: GatherlinkConfig,
    telemetry: SchedulerTelemetrySnapshot,
    *,
    service_traffic: ServiceTrafficSummary | None = None,
) -> SchedulerPolicy:
    """
    Pick a conservative fallback from configured and observed path facts.

    This intentionally avoids a magic universal default. TCP-biased opaque
    tunnels start on one best path because generic multipath can damage TCP
    before the coordinator has proof that ordered multipath is safe. Other
    traffic keeps the older aggregation-friendly baselines.
    """
    service_traffic = service_traffic or service_traffic_summary(config.services)
    metrics = list(telemetry.paths.values())
    effective_bias = _effective_traffic_bias(config, service_traffic)
    if effective_bias == "tcp":
        return "single_best_path"
    if effective_bias == "udp" or service_traffic.has_mixed_known_classes:
        return "capacity_aware"
    if _has_latency_spread(metrics):
        return "latency_guarded_capacity"
    configured_capacities = [
        path.scheduler.tx_capacity_bps
        for path in config.paths
        if path.scheduler.enabled and path.scheduler.tx_capacity_bps is not None
    ]
    observed_capacities = [metric.tx_capacity_bps for metric in metrics if metric.tx_capacity_bps is not None]
    if _capacity_is_skewed(configured_capacities) or _capacity_is_skewed(observed_capacities):
        return "capacity_aware"
    return "adaptive"


def _effective_traffic_bias(config: GatherlinkConfig, service_traffic: ServiceTrafficSummary) -> str:
    """Return operator bias, or infer a coarse one from Python-owned service classes."""
    if config.scheduler.traffic_bias != "auto":
        return config.scheduler.traffic_bias
    if service_traffic.is_tcp_like_only:
        return "tcp"
    if service_traffic.is_udp_bulk_only:
        return "udp"
    return "auto"


def _choose_mixed_service_candidate(
    config: GatherlinkConfig,
    metrics: list[PathSchedulerMetrics],
    signals: tuple[str, ...],
    fallback: SchedulerPolicy,
    service_traffic: ServiceTrafficSummary,
) -> tuple[SchedulerPolicy, str, tuple[str, ...]]:
    """
    Choose a node baseline for mixed protected/bulk services.

    Per-service path allocation handles protected-service stickiness and bulk
    expansion. The node-wide policy should therefore remain aggregation-friendly
    unless path health itself says otherwise.
    """
    if service_traffic.protected_degraded and (_has_queue_pressure(metrics) or _has_high_loss(metrics)):
        return "single_best_path", "mixed service: protected degradation with path pressure favors safety", signals
    if _has_high_jitter(metrics) and _has_latency_spread(metrics) and _has_capacity_hints(config, metrics):
        return "flowlet_adaptive", "mixed service: jitter pressure favors flowlet stickiness", signals
    if _has_latency_spread(metrics) and _has_queue_pressure(metrics):
        return (
            "arrival_guarded_capacity",
            "mixed service: queue pressure with latency spread favors arrival guard",
            signals,
        )
    if _has_queue_pressure(metrics) or _has_high_loss(metrics):
        return "capacity_aware", "mixed service: path pressure favors capacity-aware health guards", signals
    if _has_capacity_hints(config, metrics):
        return "capacity_aware", "mixed service: capacity hints favor bulk-capable baseline", signals
    return fallback, "mixed service: using known-good fallback", signals


def _has_capacity_hints(config: GatherlinkConfig, metrics: list[PathSchedulerMetrics]) -> bool:
    return any(path.scheduler.tx_capacity_bps for path in config.paths) or any(
        metric.tx_capacity_bps for metric in metrics
    )


def _telemetry_is_stale(metrics: list[PathSchedulerMetrics]) -> bool:
    stale_ages = [metric.stale_control_age_us for metric in metrics if metric.stale_control_age_us]
    return bool(stale_ages) and min(stale_ages) >= COORDINATED_STALE_CONTROL_US


def _has_high_reorder_pressure(metrics: list[PathSchedulerMetrics]) -> bool:
    return any(
        metric.receive_gaps >= COORDINATED_HIGH_REORDER_PACKETS
        or metric.reorder_depth_packets >= COORDINATED_HIGH_REORDER_PACKETS
        for metric in metrics
    )


def _has_reorder_or_loss(metrics: list[PathSchedulerMetrics]) -> bool:
    return _has_high_reorder_pressure(metrics) or _has_high_loss(metrics)


def _has_high_loss(metrics: list[PathSchedulerMetrics]) -> bool:
    return any(metric.loss_ppm >= COORDINATED_HIGH_LOSS_PPM or metric.local_drops > 0 for metric in metrics)


def _has_queue_pressure(metrics: list[PathSchedulerMetrics]) -> bool:
    return any(metric.queue_depth_packets >= COORDINATED_QUEUE_PRESSURE_PACKETS for metric in metrics)


def _has_latency_spread(metrics: list[PathSchedulerMetrics]) -> bool:
    latencies = [metric.scheduler_latency_us for metric in metrics if metric.scheduler_latency_us is not None]
    if len(latencies) < 2:
        return False
    fastest = min(latencies)
    slowest = max(latencies)
    return slowest - fastest >= max(
        COORDINATED_LATENCY_GUARD_MIN_SPREAD_US,
        fastest * COORDINATED_LATENCY_GUARD_SPREAD_MULTIPLIER,
    )


def _has_high_jitter(metrics: list[PathSchedulerMetrics]) -> bool:
    return any(
        (metric.tx_jitter_us is not None and metric.tx_jitter_us >= COORDINATED_HIGH_JITTER_US)
        or (metric.rx_jitter_us is not None and metric.rx_jitter_us >= COORDINATED_HIGH_JITTER_US)
        for metric in metrics
    )


def _path_jitter_us(metric: PathSchedulerMetrics) -> int | None:
    """Return the largest directional jitter value for coordinator decisions."""
    known = [value for value in (metric.tx_jitter_us, metric.rx_jitter_us) if value is not None]
    return max(known) if known else None


def _metric_or_config_capacity(config: GatherlinkConfig, metric: PathSchedulerMetrics) -> int | None:
    """Return telemetry capacity or the matching configured startup hint."""
    if metric.tx_capacity_bps is not None:
        return metric.tx_capacity_bps
    for path in config.paths:
        if path.name == metric.path_name:
            return path.scheduler.tx_capacity_bps
    return None


def _capacity_confidence_is_high(metrics: list[PathSchedulerMetrics]) -> bool:
    confidence_values = [capacity_confidence_ppm(metric) for metric in metrics if metric.tx_capacity_bps is not None]
    if metrics and not confidence_values:
        return True
    return bool(confidence_values) and min(confidence_values) >= CAPACITY_CONFIDENCE_HIGH_PPM


def _telemetry_signals(config: GatherlinkConfig, metrics: list[PathSchedulerMetrics]) -> tuple[str, ...]:
    """Return compact reason facts without producing noisy per-packet logs."""
    signals: list[str] = []
    if not metrics:
        signals.append("no_telemetry")
    if _telemetry_is_stale(metrics):
        signals.append("stale_control")
    if _has_capacity_hints(config, metrics):
        signals.append("capacity_hints")
    if _capacity_is_skewed([metric.tx_capacity_bps for metric in metrics]):
        signals.append("observed_capacity_skew")
    if _has_latency_spread(metrics):
        signals.append("latency_spread")
    if _has_high_jitter(metrics):
        signals.append("jitter_pressure")
    if any(
        metric.latency_source == "clock-synced-one-way"
        and metric.latency_clock_error_us is not None
        and metric.latency_clock_error_us > COORDINATED_TCP_ORDERED_MAX_CLOCK_ERROR_US
        for metric in metrics
    ):
        signals.append("latency_uncertainty")
    if _tcp_ordered_latency_quality_is_too_noisy(metrics):
        signals.append("tcp_ordered_latency_risk")
    if _has_high_reorder_pressure(metrics):
        signals.append("reorder_pressure")
    if _has_queue_pressure(metrics):
        signals.append("queue_pressure")
    if _has_high_loss(metrics):
        signals.append("loss_pressure")
    if metrics and _capacity_confidence_is_high(metrics):
        signals.append("capacity_confident")
    signals.extend(f"profile:{profile}" for profile in path_responsiveness_summary(metrics))
    return tuple(dict.fromkeys(signals))


def path_responsiveness_summary(metrics: list[PathSchedulerMetrics]) -> tuple[PathResponsivenessClass, ...]:
    """Return the path responsiveness classes represented in one telemetry window."""
    return tuple(dict.fromkeys(classify_path_responsiveness(metric) for metric in metrics))


def _stable_responsiveness_class(
    history: deque[PathResponsivenessClass],
) -> tuple[PathResponsivenessClass, int]:
    """Return the most repeated class and confidence from bounded history."""
    if not history:
        return "unknown", 0
    counts = {profile: 0 for profile in PATH_RESPONSIVENESS_CLASSES}
    for profile in history:
        counts[profile] = counts.get(profile, 0) + 1
    stable_class, count = max(counts.items(), key=lambda item: (item[1], item[0] != "unknown"))
    confidence_ppm = count * 1_000_000 // len(history)
    return stable_class, confidence_ppm


def classify_path_responsiveness(metric: PathSchedulerMetrics) -> PathResponsivenessClass:
    """
    Classify one path from scheduler-visible facts.

    This is deliberately a weak, explainable hint. Fixed operator config and
    direct scheduler telemetry still win. The class exists so Python can choose
    safer defaults for real-world profiles without putting access-type meaning
    into Rust.
    """
    latency_us = metric.scheduler_latency_us
    jitter_us = _path_jitter_us(metric) or 0
    if metric.loss_ppm >= RESPONSIVENESS_VOLATILE_MIN_LOSS_PPM or jitter_us >= RESPONSIVENESS_VOLATILE_MIN_JITTER_US:
        return "volatile_wireless"
    if latency_us is None:
        return "unknown"
    if (
        latency_us <= RESPONSIVENESS_WIRED_MAX_LATENCY_US
        and jitter_us <= RESPONSIVENESS_WIRED_MAX_JITTER_US
        and metric.loss_ppm == 0
        and metric.queue_depth_packets == 0
    ):
        return "wired_stable"
    if latency_us >= RESPONSIVENESS_STARLINK_MIN_LATENCY_US and jitter_us >= RESPONSIVENESS_STARLINK_MIN_JITTER_US:
        return "starlink_like"
    if latency_us >= RESPONSIVENESS_CELLULAR_MIN_LATENCY_US or jitter_us >= RESPONSIVENESS_CELLULAR_MIN_JITTER_US:
        return "cellular_like"
    return "unknown"


def _capacity_is_skewed(capacities: list[int | None]) -> bool:
    known = [capacity for capacity in capacities if capacity is not None and capacity > 0]
    if len(known) < 2:
        return False
    return max(known) * COORDINATED_CAPACITY_SKEW_DENOMINATOR >= min(known) * COORDINATED_CAPACITY_SKEW_NUMERATOR


def _observed_packets(telemetry: SchedulerTelemetrySnapshot) -> int:
    """Return the packet-volume clock used for coordinator dwell."""
    return sum(metric.observed_packets for metric in telemetry.paths.values())
