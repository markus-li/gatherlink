"""Service priority helpers owned by the Python scheduler/control plane."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from gatherlink.config.runtime import RuntimeServiceConfig

SERVICE_PRIORITY_POLL_SLOTS = {
    "bulk": 1,
    "normal": 2,
    "high": 3,
    "critical": 4,
}


def service_poll_order(services: list[RuntimeServiceConfig]) -> list[str]:
    """
    Return a bounded fair service poll order for Rust runner calls.

    Python owns the meaning of service priority. The order always includes every
    listening service at least once to avoid starvation, then repeats higher
    priority services a small bounded number of times so their traffic is not
    invisible under shared runner pressure.
    """
    ordered: list[str] = []
    for service in services:
        if not service.listen:
            continue
        slots = SERVICE_PRIORITY_POLL_SLOTS[service.priority]
        ordered.extend([service.name] * slots)
    return ordered


def service_poll_plan(
    services: Sequence[RuntimeServiceConfig],
    batch_size: int,
    packet_budget_overrides: Mapping[str, int] | None = None,
) -> list[tuple[str, int]]:
    """
    Return a bounded service drain plan for Rust runner calls.

    This is the more precise companion to :func:`service_poll_order`. Python
    still owns service meaning and QoS. Rust receives only service names and a
    per-slot packet quantum, then executes the requested nonblocking drains.
    """
    plan: list[tuple[str, int]] = []
    packet_budget_overrides = packet_budget_overrides or {}
    for service in services:
        if not service.listen:
            continue
        slots = SERVICE_PRIORITY_POLL_SLOTS[service.priority]
        packet_budget = int(
            packet_budget_overrides.get(service.name) or service.scheduler_poll_batch_packets or batch_size
        )
        plan.extend((service.name, packet_budget) for _ in range(slots))
    return plan


def service_budget_plan(
    services: Sequence[RuntimeServiceConfig],
    batch_size: int,
    packet_budget_overrides: Mapping[str, int] | None = None,
    byte_budget_overrides: Mapping[str, int] | None = None,
) -> list[tuple[str, int, int]]:
    """
    Return a service drain plan with optional byte caps.

    Packet and byte budgets remain Python-owned policy. Rust receives only the
    primitive values to execute for each service slot.
    """
    packet_budget_overrides = packet_budget_overrides or {}
    byte_budget_overrides = byte_budget_overrides or {}
    plan: list[tuple[str, int, int]] = []
    for service in services:
        if not service.listen:
            continue
        slots = SERVICE_PRIORITY_POLL_SLOTS[service.priority]
        packet_budget = int(
            packet_budget_overrides.get(service.name) or service.scheduler_poll_batch_packets or batch_size
        )
        byte_budget = int(byte_budget_overrides.get(service.name) or 0)
        plan.extend((service.name, packet_budget, byte_budget) for _ in range(slots))
    return plan


def uses_service_drain_plan(
    services: Sequence[RuntimeServiceConfig],
    packet_budget_overrides: Mapping[str, int] | None = None,
) -> bool:
    """Return whether Python has compiled any non-default per-service drain plan."""
    packet_budget_overrides = packet_budget_overrides or {}
    return any(
        bool(service.listen)
        and (bool(service.scheduler_poll_batch_packets) or int(packet_budget_overrides.get(service.name) or 0) > 0)
        for service in services
    )


def uses_service_budget_plan(
    services: Sequence[RuntimeServiceConfig],
    packet_budget_overrides: Mapping[str, int] | None = None,
    byte_budget_overrides: Mapping[str, int] | None = None,
) -> bool:
    """Return whether Python has compiled byte/time service budget primitives."""
    packet_budget_overrides = packet_budget_overrides or {}
    byte_budget_overrides = byte_budget_overrides or {}
    return any(
        bool(service.listen)
        and (
            bool(service.scheduler_poll_batch_packets)
            or int(packet_budget_overrides.get(service.name) or 0) > 0
            or int(byte_budget_overrides.get(service.name) or 0) > 0
        )
        for service in services
    )
