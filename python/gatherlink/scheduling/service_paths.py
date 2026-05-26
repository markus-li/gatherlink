"""
Python-owned dynamic service path allocation.

Rust already has the cheap primitives needed to execute a per-service path
plan: allowed path ids, per-path weights, and the service path policy. This
module decides *when* those primitives should move. The goal is to keep fixed
path splits available for advanced/debug setups while making `coordinated_adaptive`
able to react when a path goes down, a service saturates its current path set,
or a mixed traffic profile needs a different split than the initial hints.
"""

from __future__ import annotations

from dataclasses import dataclass

from gatherlink.config.models import GatherlinkConfig
from gatherlink.config.runtime import RuntimeConfig, RuntimePathConfig, RuntimeServiceConfig
from gatherlink.scheduling.metrics import PathSchedulerMetrics, SchedulerTelemetrySnapshot
from gatherlink.scheduling.service_intent import service_is_bulk, service_is_protected

SERVICE_PATH_MIN_SAMPLE_SECONDS = 0.50
SERVICE_PATH_MIN_DWELL_SECONDS = 5.0
SERVICE_PATH_HEADROOM = 1.05
SERVICE_PATH_EXPAND_HIGH_WATERMARK = 0.95
SERVICE_PATH_HIGH_LOSS_PPM = 80_000
SERVICE_PATH_QUEUE_PRESSURE_PACKETS = 512
SERVICE_PATH_STALE_CONTROL_US = 120_000_000
SERVICE_PATH_MIN_WEIGHT = 1
SERVICE_PATH_MAX_WEIGHT = 65_535


@dataclass(frozen=True)
class ServicePathRateSample:
    """One service's transmit rate derived from service counters."""

    service: str
    priority: str
    traffic_class: str
    tx_bytes_per_second: float


@dataclass(frozen=True)
class ServicePathPlan:
    """Compiled service path primitive overrides for one service."""

    service: str
    path_policy: str
    allowed_path_ids: tuple[int, ...]
    path_weights: tuple[tuple[int, int], ...]
    reason: str


@dataclass(frozen=True)
class ServicePathAllocationDecision:
    """Result of one allocation pass."""

    services: list[RuntimeServiceConfig]
    changed: bool
    reason: str
    plans: tuple[ServicePathPlan, ...]
    samples: tuple[ServicePathRateSample, ...] = ()

    def export_dict(self) -> dict[str, object]:
        """Return a bounded diagnostic payload for operator-visible decisions."""
        return {
            "changed": self.changed,
            "reason": self.reason,
            "plans": [
                {
                    "service": plan.service,
                    "path_policy": plan.path_policy,
                    "allowed_path_ids": list(plan.allowed_path_ids),
                    "path_weights": [list(weight) for weight in plan.path_weights],
                    "reason": plan.reason,
                }
                for plan in self.plans
            ],
            "samples": [
                {
                    "service": sample.service,
                    "priority": sample.priority,
                    "traffic_class": sample.traffic_class,
                    "tx_bytes_per_second": round(sample.tx_bytes_per_second, 2),
                }
                for sample in self.samples
            ],
        }


@dataclass
class _PreviousServiceCounter:
    """Service counter retained between reapply windows."""

    observed_at: float
    tx_bytes: int


class ServicePathAllocator:
    """
    Recompile per-service path choices from live path and service telemetry.

    This controller is deliberately conservative. It only runs for
    `coordinated_adaptive`, uses dwell time before replacing an existing plan,
    and keeps Rust-facing output to existing path-policy/allowed-path/weight
    primitives. Python remains the only place where service priority, traffic
    mix, and operator meaning are interpreted.
    """

    def __init__(self) -> None:
        """Create an allocator with no prior service rate samples."""
        self._previous: dict[str, _PreviousServiceCounter] = {}
        self._active_plans: dict[str, ServicePathPlan] = {}
        self._last_change_at: dict[str, float] = {}
        self.last_decision: ServicePathAllocationDecision | None = None

    def update(
        self,
        config: GatherlinkConfig,
        runtime_config: RuntimeConfig,
        telemetry: SchedulerTelemetrySnapshot,
        status: dict[str, object],
        *,
        now: float,
    ) -> ServicePathAllocationDecision:
        """Return services with dynamic path primitives applied where useful."""
        if config.scheduler.mode != "coordinated_adaptive":
            decision = ServicePathAllocationDecision(
                list(runtime_config.services),
                False,
                "service path allocation disabled outside coordinated_adaptive",
                (),
            )
            self.last_decision = decision
            return decision
        services = list(runtime_config.services)
        eligible = [service for service in services if service.listen]
        if len(runtime_config.paths) < 2 or not eligible:
            decision = ServicePathAllocationDecision(services, False, "not enough paths or listening services", ())
            self.last_decision = decision
            return decision

        service_stats = status.get("service_stats")
        samples = self._rate_samples(eligible, service_stats if isinstance(service_stats, dict) else {}, now)
        path_by_id = {path.scheduler.path_id: path for path in runtime_config.paths}
        healthy_paths = _healthy_paths(runtime_config.paths, telemetry)
        if not healthy_paths:
            decision = ServicePathAllocationDecision(services, False, "no healthy paths available", (), tuple(samples))
            self.last_decision = decision
            return decision

        plans: list[ServicePathPlan] = []
        updated_services: list[RuntimeServiceConfig] = []
        degraded_services = _degraded_services(status)
        high_plan_paths = self._high_priority_path_ids(eligible, healthy_paths, telemetry)
        sample_by_service = {sample.service: sample for sample in samples}
        reserved_bulk_bps: dict[int, int] = {}

        for service in services:
            sample = sample_by_service.get(service.name)
            plan = self._plan_for_service(
                service,
                healthy_paths=healthy_paths,
                high_plan_paths=high_plan_paths,
                sample=sample,
                telemetry=telemetry,
                degraded=service.name in degraded_services,
                path_by_id=path_by_id,
                reserved_bulk_bps=reserved_bulk_bps,
                now=now,
            )
            plans.append(plan)
            updated_services.append(_apply_plan(service, plan))
            if service_is_bulk(service) and sample is not None:
                _reserve_bulk_capacity(
                    reserved_bulk_bps,
                    plan,
                    path_by_id,
                    int(sample.tx_bytes_per_second * 8 * SERVICE_PATH_HEADROOM),
                )

        changed = any(
            _service_path_tuple(old) != _service_path_tuple(new)
            for old, new in zip(services, updated_services, strict=True)
        )
        reason = (
            "; ".join(plan.reason for plan in plans if _plan_changes_service(services, plan))
            or "service path plan held"
        )
        decision = ServicePathAllocationDecision(updated_services, changed, reason, tuple(plans), tuple(samples))
        self.last_decision = decision
        return decision

    def _rate_samples(
        self,
        services: list[RuntimeServiceConfig],
        service_stats: dict[object, object],
        now: float,
    ) -> list[ServicePathRateSample]:
        """Convert service tx byte counters into rates for allocation decisions."""
        samples: list[ServicePathRateSample] = []
        for service in services:
            raw = service_stats.get(service.name)
            if not isinstance(raw, dict):
                continue
            current = _PreviousServiceCounter(
                observed_at=now,
                tx_bytes=max(0, _int_or_zero(raw.get("tx_bytes"))),
            )
            previous = self._previous.get(service.name)
            if previous is None:
                self._previous[service.name] = current
                continue
            elapsed = current.observed_at - previous.observed_at
            if elapsed < SERVICE_PATH_MIN_SAMPLE_SECONDS:
                continue
            self._previous[service.name] = current
            samples.append(
                ServicePathRateSample(
                    service=service.name,
                    priority=service.priority,
                    traffic_class=service.traffic_class,
                    tx_bytes_per_second=max(0, current.tx_bytes - previous.tx_bytes) / elapsed,
                )
            )
        return samples

    def _high_priority_path_ids(
        self,
        services: list[RuntimeServiceConfig],
        healthy_paths: list[RuntimePathConfig],
        telemetry: SchedulerTelemetrySnapshot,
    ) -> set[int]:
        """Reserve current/best paths used by conservative services when possible."""
        reserved: set[int] = set()
        for service in services:
            if not service_is_protected(service):
                continue
            current_ids = [
                path_id for path_id in service.scheduler_allowed_path_ids if path_id in _path_id_set(healthy_paths)
            ]
            if current_ids:
                reserved.add(
                    _best_path(
                        [path for path in healthy_paths if path.scheduler.path_id in current_ids], telemetry
                    ).scheduler.path_id
                )
                continue
            reserved.add(_best_path(healthy_paths, telemetry).scheduler.path_id)
        return reserved

    def _plan_for_service(
        self,
        service: RuntimeServiceConfig,
        *,
        healthy_paths: list[RuntimePathConfig],
        high_plan_paths: set[int],
        sample: ServicePathRateSample | None,
        telemetry: SchedulerTelemetrySnapshot,
        degraded: bool,
        path_by_id: dict[int, RuntimePathConfig],
        reserved_bulk_bps: dict[int, int],
        now: float,
    ) -> ServicePathPlan:
        """Compile one service path plan from priority, pressure, and path health."""
        if service_is_protected(service):
            return self._protected_service_plan(service, healthy_paths, telemetry, degraded, now)
        if service_is_bulk(service):
            return self._bulk_service_plan(
                service,
                healthy_paths,
                high_plan_paths,
                sample,
                telemetry,
                path_by_id,
                reserved_bulk_bps,
                now,
            )
        return self._normal_service_plan(service, healthy_paths, telemetry, now)

    def _protected_service_plan(
        self,
        service: RuntimeServiceConfig,
        healthy_paths: list[RuntimePathConfig],
        telemetry: SchedulerTelemetrySnapshot,
        degraded: bool,
        now: float,
    ) -> ServicePathPlan:
        """Keep TCP-like/protected services sticky, but fail over when needed."""
        current = [path for path in healthy_paths if path.scheduler.path_id in service.scheduler_allowed_path_ids]
        chosen = _best_path(current or healthy_paths, telemetry)
        reason = (
            f"{service.name}: protected service outcome degraded but path counters are still healthy; holding "
            f"{chosen.name}"
            if degraded and current
            else f"{service.name}: protected service uses healthy best path {chosen.name}"
        )
        plan = ServicePathPlan(
            service=service.name,
            path_policy="single_best_path",
            allowed_path_ids=(chosen.scheduler.path_id,),
            path_weights=((chosen.scheduler.path_id, _path_weight(chosen)),),
            reason=reason,
        )
        return self._dwell_or_plan(service, plan, now)

    def _normal_service_plan(
        self,
        service: RuntimeServiceConfig,
        healthy_paths: list[RuntimePathConfig],
        telemetry: SchedulerTelemetrySnapshot,
        now: float,
    ) -> ServicePathPlan:
        """Keep ordinary services on their current primitive set unless it is unusable."""
        allowed = [path for path in healthy_paths if path.scheduler.path_id in service.scheduler_allowed_path_ids]
        if not service.scheduler_allowed_path_ids or allowed:
            return ServicePathPlan(
                service=service.name,
                path_policy=service.scheduler_path_policy,
                allowed_path_ids=tuple(service.scheduler_allowed_path_ids),
                path_weights=tuple(service.scheduler_path_weights),
                reason=f"{service.name}: normal service held configured path plan",
            )
        chosen = [_best_path(healthy_paths, telemetry)]
        plan = ServicePathPlan(
            service=service.name,
            path_policy="single_best_path",
            allowed_path_ids=tuple(path.scheduler.path_id for path in chosen),
            path_weights=tuple((path.scheduler.path_id, _path_weight(path)) for path in chosen),
            reason=f"{service.name}: normal service failed over from unhealthy configured paths",
        )
        return self._dwell_or_plan(service, plan, now)

    def _bulk_service_plan(
        self,
        service: RuntimeServiceConfig,
        healthy_paths: list[RuntimePathConfig],
        high_plan_paths: set[int],
        sample: ServicePathRateSample | None,
        telemetry: SchedulerTelemetrySnapshot,
        path_by_id: dict[int, RuntimePathConfig],
        reserved_bulk_bps: dict[int, int],
        now: float,
    ) -> ServicePathPlan:
        """Let bulk/fast services expand and shrink based on observed demand."""
        spare_paths = [path for path in healthy_paths if path.scheduler.path_id not in high_plan_paths]
        candidates = spare_paths or healthy_paths
        current = [path_by_id[path_id] for path_id in service.scheduler_allowed_path_ids if path_id in path_by_id]
        if sample is None:
            chosen = [path for path in current if path.scheduler.path_id in _path_id_set(candidates)] or [
                _best_path(candidates, telemetry)
            ]
            reason = f"{service.name}: waiting for service rate sample"
        else:
            target_bps = int(sample.tx_bytes_per_second * 8 * SERVICE_PATH_HEADROOM)
            current_healthy = [path for path in current if path.scheduler.path_id in _path_id_set(candidates)]
            current_capacity = sum(_path_available_bps(path, reserved_bulk_bps) for path in current_healthy)
            if len(current_healthy) == 1 and target_bps <= int(current_capacity * SERVICE_PATH_EXPAND_HIGH_WATERMARK):
                chosen = current_healthy
                reason = (
                    f"{service.name}: {target_bps}bps demand target fits current "
                    f"{','.join(path.name for path in chosen)}"
                )
            else:
                chosen = _smallest_capacity_set(candidates, target_bps, reserved_bulk_bps=reserved_bulk_bps)
                reason = (
                    f"{service.name}: {target_bps}bps demand target compiled to "
                    f"{','.join(path.name for path in chosen)}"
                )
        plan = ServicePathPlan(
            service=service.name,
            path_policy="weighted_round_robin",
            allowed_path_ids=tuple(path.scheduler.path_id for path in chosen),
            path_weights=tuple((path.scheduler.path_id, _path_weight(path)) for path in chosen),
            reason=reason,
        )
        return self._dwell_or_plan(service, plan, now)

    def _dwell_or_plan(self, service: RuntimeServiceConfig, plan: ServicePathPlan, now: float) -> ServicePathPlan:
        """Apply a minimum dwell guard before changing an active plan."""
        current_tuple = _service_path_tuple(service)
        plan_tuple = (plan.path_policy, plan.allowed_path_ids, plan.path_weights)
        if current_tuple == plan_tuple:
            self._active_plans[service.name] = plan
            self._last_change_at.setdefault(service.name, now)
            return plan
        last_change = self._last_change_at.get(service.name)
        if last_change is not None and now - last_change < SERVICE_PATH_MIN_DWELL_SECONDS:
            held = self._active_plans.get(service.name)
            if held is not None:
                return ServicePathPlan(
                    service=service.name,
                    path_policy=held.path_policy,
                    allowed_path_ids=held.allowed_path_ids,
                    path_weights=held.path_weights,
                    reason=f"{service.name}: holding previous service path plan during dwell guard",
                )
        self._active_plans[service.name] = plan
        self._last_change_at[service.name] = now
        return plan


def _healthy_paths(
    paths: list[RuntimePathConfig],
    telemetry: SchedulerTelemetrySnapshot,
) -> list[RuntimePathConfig]:
    """Return paths that can reasonably carry new service allocations."""
    healthy: list[RuntimePathConfig] = []
    for path in paths:
        scheduler = path.scheduler
        metric = telemetry.paths.get(path.name)
        if not scheduler.enabled or scheduler.state == "down":
            continue
        if metric is not None and _metric_is_unhealthy(metric):
            continue
        healthy.append(path)
    return healthy


def _metric_is_unhealthy(metric: PathSchedulerMetrics) -> bool:
    """Return whether live telemetry should remove a path from service plans."""
    return (
        metric.loss_ppm >= SERVICE_PATH_HIGH_LOSS_PPM
        or metric.queue_depth_packets >= SERVICE_PATH_QUEUE_PRESSURE_PACKETS
        or metric.send_failures > 0
        or metric.local_drops > 0
        or metric.stale_control_age_us >= SERVICE_PATH_STALE_CONTROL_US
    )


def _smallest_capacity_set(
    paths: list[RuntimePathConfig],
    target_bps: int,
    *,
    reserved_bulk_bps: dict[int, int] | None = None,
) -> list[RuntimePathConfig]:
    """Choose the smallest high-capacity healthy path set that covers demand."""
    reserved_bulk_bps = reserved_bulk_bps or {}
    ranked = sorted(paths, key=lambda path: _path_available_bps(path, reserved_bulk_bps), reverse=True)
    if target_bps <= 0:
        return ranked[:1]
    chosen: list[RuntimePathConfig] = []
    total = 0
    for path in ranked:
        chosen.append(path)
        total += _path_available_bps(path, reserved_bulk_bps)
        if total >= target_bps:
            break
    return chosen or ranked[:1]


def _reserve_bulk_capacity(
    reserved_bulk_bps: dict[int, int],
    plan: ServicePathPlan,
    path_by_id: dict[int, RuntimePathConfig],
    target_bps: int,
) -> None:
    """Reserve a planned bulk service load so later bulk plans see less spare capacity."""
    planned_paths = [path_by_id[path_id] for path_id in plan.allowed_path_ids if path_id in path_by_id]
    if not planned_paths or target_bps <= 0:
        return
    total_weight = sum(max(1, weight) for path_id, weight in plan.path_weights if path_id in path_by_id) or len(
        planned_paths
    )
    for path in planned_paths:
        weight = next((max(1, value) for path_id, value in plan.path_weights if path_id == path.scheduler.path_id), 1)
        reserved_bulk_bps[path.scheduler.path_id] = reserved_bulk_bps.get(path.scheduler.path_id, 0) + (
            target_bps * weight // total_weight
        )


def _path_available_bps(path: RuntimePathConfig, reserved_bulk_bps: dict[int, int]) -> int:
    """Return path capacity after earlier same-pass bulk reservations."""
    return max(1, _path_capacity_bps(path) - reserved_bulk_bps.get(path.scheduler.path_id, 0))


def _best_path(paths: list[RuntimePathConfig], telemetry: SchedulerTelemetrySnapshot) -> RuntimePathConfig:
    """Return the best path using capacity first and latency as a tie-breaker."""
    return sorted(
        paths,
        key=lambda path: (
            -_path_capacity_bps(path),
            _path_latency_us(path, telemetry),
            path.name,
        ),
    )[0]


def _path_capacity_bps(path: RuntimePathConfig) -> int:
    """Return configured/detected capacity with a safe nonzero fallback."""
    return int(path.scheduler.tx_capacity_bps or path.scheduler.rx_capacity_bps or 1)


def _path_latency_us(path: RuntimePathConfig, telemetry: SchedulerTelemetrySnapshot) -> int:
    """Return scheduler latency with a large fallback for unknown values."""
    metric = telemetry.paths.get(path.name)
    if metric is not None and metric.scheduler_latency_us is not None:
        return metric.scheduler_latency_us
    if path.scheduler.latency_us is not None:
        return path.scheduler.latency_us
    return 1_000_000_000


def _path_weight(path: RuntimePathConfig) -> int:
    """Compile capacity into Rust's compact u16 service weight."""
    mbits = max(SERVICE_PATH_MIN_WEIGHT, round(_path_capacity_bps(path) / 1_000_000))
    return min(SERVICE_PATH_MAX_WEIGHT, mbits)


def _path_id_set(paths: list[RuntimePathConfig]) -> set[int]:
    """Return compact path ids for membership checks."""
    return {path.scheduler.path_id for path in paths}


def _apply_plan(service: RuntimeServiceConfig, plan: ServicePathPlan) -> RuntimeServiceConfig:
    """Return a service with Rust-executable path primitives from a plan."""
    return service.model_copy(
        update={
            "scheduler_path_policy": plan.path_policy,
            "scheduler_allowed_path_ids": list(plan.allowed_path_ids),
            "scheduler_path_weights": list(plan.path_weights),
        }
    )


def _service_path_tuple(service: RuntimeServiceConfig) -> tuple[str, tuple[int, ...], tuple[tuple[int, int], ...]]:
    """Return only the per-service path primitives."""
    return (
        service.scheduler_path_policy,
        tuple(service.scheduler_allowed_path_ids),
        tuple(service.scheduler_path_weights),
    )


def _plan_changes_service(services: list[RuntimeServiceConfig], plan: ServicePathPlan) -> bool:
    """Return whether the plan differs from the matching service."""
    for service in services:
        if service.name == plan.service:
            return _service_path_tuple(service) != (plan.path_policy, plan.allowed_path_ids, plan.path_weights)
    return False


def _degraded_services(status: dict[str, object]) -> set[str]:
    """Return services whose Python-side outcome feedback is currently degraded."""
    raw = status.get("service_outcomes")
    if not isinstance(raw, list):
        return set()
    degraded: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        service = entry.get("service")
        if isinstance(service, str) and bool(entry.get("degraded")):
            degraded.add(service)
    return degraded


def _int_or_zero(value: object) -> int:
    """Convert loose service counters into safe non-negative integers."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
