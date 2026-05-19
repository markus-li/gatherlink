"""
Bridge expanded Python runtime state into the Rust dataplane bindings.

This module is intentionally production-owned, not lab-owned. Python has already
validated user config, expanded defaults, assigned service priorities, and
compiled scheduler policy before values arrive here. The Rust extension receives
only narrow DTOs and executes them without interpreting business policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any

from gatherlink.config.runtime import RuntimeConfig, RuntimePathSchedulerConfig, RuntimeServiceConfig


class RustDataplaneUnavailableError(RuntimeError):
    """Raised when the optional Rust PyO3 dataplane extension is not installed."""


class RustRuntimeBridgeError(ValueError):
    """Raised when expanded runtime state cannot fit the Rust DTO contract."""


@dataclass(frozen=True)
class RustRuntimeDtos:
    """DTOs ready to pass into the Rust PyO3 dataplane API."""

    services: list[Any]
    paths: list[Any]
    scheduler: Any
    security: Any


def bind_core_dataplane(runtime_config: RuntimeConfig) -> Any:
    """Bind a Rust core dataplane from already-expanded runtime config."""
    bindings = _load_bindings()
    dtos = build_rust_runtime_dtos(runtime_config, bindings=bindings)
    return bindings.CoreDataplane.bind_with_scheduler(dtos.services, dtos.paths, dtos.scheduler, dtos.security)


def reapply_core_dataplane(dataplane: Any, runtime_config: RuntimeConfig) -> Any:
    """Hot-reapply already-expanded runtime config to an existing Rust dataplane."""
    bindings = _load_bindings()
    dtos = build_rust_runtime_dtos(runtime_config, bindings=bindings)
    return dataplane.reapply_config_with_scheduler(dtos.services, dtos.paths, dtos.scheduler, dtos.security)


def reapply_core_scheduler(dataplane: Any, runtime_config: RuntimeConfig) -> Any:
    """Hot-reapply scheduler/path primitives without rebinding live Rust sockets."""
    bindings = _load_bindings()
    dtos = build_rust_runtime_dtos(runtime_config, bindings=bindings)
    return dataplane.reapply_scheduler(dtos.paths, dtos.scheduler)


def build_rust_runtime_dtos(
    runtime_config: RuntimeConfig, *, bindings: ModuleType | Any | None = None
) -> RustRuntimeDtos:
    """Convert Python runtime models into the narrow Rust binding DTOs."""
    bindings = bindings or _load_bindings()
    return RustRuntimeDtos(
        services=[_service_dto(bindings, service) for service in runtime_config.services],
        paths=[_path_dto(bindings, path) for path in runtime_config.paths],
        scheduler=bindings.SchedulerConfig(runtime_config.scheduler.mode),
        security=_security_dto(bindings, runtime_config.security),
    )


def _load_bindings() -> ModuleType:
    """Import the optional Rust extension with an actionable Python-side error."""
    try:
        return import_module("gatherlink_pybindings")
    except ModuleNotFoundError as exc:
        raise RustDataplaneUnavailableError(
            "Rust dataplane bindings are not installed. Build them with maturin before starting a Rust-backed "
            "Gatherlink core service."
        ) from exc


def _service_dto(bindings: ModuleType | Any, service: RuntimeServiceConfig) -> Any:
    """Build one Rust service DTO after checking integer bounds owned by the bridge."""
    return bindings.UdpServiceConfig(
        service.name,
        service.target,
        service.listen,
        _bounded_u16(service.priority_value, field="service.priority_value"),
        service.return_mode,
        _bounded_user_service_id(service.service_id),
        _bounded_u16(service.scheduler_fanout, field="service.scheduler_fanout"),
        service.scheduler_fanout_below_bytes,
    )


def _path_dto(bindings: ModuleType | Any, path: Any) -> Any:
    """Build one Rust path DTO from compiled scheduler state."""
    scheduler: RuntimePathSchedulerConfig = path.scheduler
    return bindings.PathConfig(
        _bounded_u16(scheduler.path_id, field="path.scheduler.path_id"),
        scheduler.mtu,
        scheduler.state == "busy",
        scheduler.enabled,
        scheduler.state,
        _bounded_u16(scheduler.weight, field="path.scheduler.weight"),
        _optional_u64(scheduler.tx_capacity_bps, field="path.scheduler.tx_capacity_bps"),
        _optional_u64(scheduler.rx_capacity_bps, field="path.scheduler.rx_capacity_bps"),
        _optional_u32(scheduler.latency_us, field="path.scheduler.latency_us"),
        _bounded_u32(scheduler.loss_ppm, field="path.scheduler.loss_ppm"),
        _bounded_u32(scheduler.reorder_hold_us, field="path.scheduler.reorder_hold_us"),
        _bounded_u16(scheduler.max_in_flight_packets, field="path.scheduler.max_in_flight_packets"),
        _bounded_u32(scheduler.max_in_flight_bytes, field="path.scheduler.max_in_flight_bytes"),
        path.transport_bind,
        path.transport_remote,
    )


def _security_dto(bindings: ModuleType | Any, security: Any) -> Any:
    """Build one Rust transport-security DTO from compiled runtime security."""
    if security.mode == "none":
        return bindings.TransportSecurityConfig.none()
    if security.mode == "static":
        if security.send_key is None or security.receive_key is None:
            raise RustRuntimeBridgeError("security.mode=static requires compiled send_key and receive_key")
        return bindings.TransportSecurityConfig.static_keys(
            _bounded_u32(security.receiver_index, field="security.receiver_index"),
            security.send_key,
            security.receive_key,
        )
    raise RustRuntimeBridgeError(f"unsupported security mode: {security.mode}")


def _optional_u64(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    return _bounded(value, field=field, minimum=0, maximum=2**64 - 1)


def _optional_u32(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    return _bounded_u32(value, field=field)


def _bounded_u16(value: int, *, field: str) -> int:
    return _bounded(value, field=field, minimum=0, maximum=2**16 - 1)


def _bounded_user_service_id(value: int) -> int:
    return _bounded(value, field="service.service_id", minimum=256, maximum=2**16 - 1)


def _bounded_u32(value: int, *, field: str) -> int:
    return _bounded(value, field=field, minimum=0, maximum=2**32 - 1)


def _bounded(value: int, *, field: str, minimum: int, maximum: int) -> int:
    """Return an integer if it fits the Rust DTO field width."""
    if value < minimum or value > maximum:
        raise RustRuntimeBridgeError(f"{field}={value} does not fit Rust DTO range {minimum}..{maximum}")
    return int(value)
