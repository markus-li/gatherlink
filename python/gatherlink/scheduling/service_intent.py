"""
Python-owned service intent helpers for scheduler policy.

Service priority and traffic class are semantic inputs. Keep that meaning here
so Rust-facing modules receive only compiled primitives such as service order,
budgets, path ids, and weights.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

PROTECTED_TRAFFIC_CLASSES = {"tcp_ordered", "latency_sensitive", "control"}
BULK_TRAFFIC_CLASSES = {"udp_bulk"}


class ServiceIntentLike(Protocol):
    """Small protocol shared by config and runtime service models."""

    name: str
    priority: str
    traffic_class: str


@dataclass(frozen=True)
class ServiceTrafficSummary:
    """Bounded summary of Python-owned service intent and current counters."""

    protected_services: tuple[str, ...] = ()
    bulk_services: tuple[str, ...] = ()
    unknown_services: tuple[str, ...] = ()
    protected_tx_bytes: int = 0
    bulk_tx_bytes: int = 0
    unknown_tx_bytes: int = 0
    protected_degraded: tuple[str, ...] = ()

    @property
    def has_protected(self) -> bool:
        """Return whether any service is order-sensitive or otherwise protected."""
        return bool(self.protected_services)

    @property
    def has_bulk(self) -> bool:
        """Return whether any service is bulk/throughput oriented."""
        return bool(self.bulk_services)

    @property
    def has_mixed_known_classes(self) -> bool:
        """Return whether protected and bulk service classes coexist."""
        return self.has_protected and self.has_bulk

    @property
    def is_tcp_like_only(self) -> bool:
        """Return whether known service classes are protected without bulk peers."""
        return self.has_protected and not self.has_bulk

    @property
    def is_udp_bulk_only(self) -> bool:
        """Return whether known service classes are bulk without protected peers."""
        return self.has_bulk and not self.has_protected

    def signals(self) -> tuple[str, ...]:
        """Return compact scheduler diagnostic signals for this service mix."""
        signals = []
        if self.is_tcp_like_only:
            signals.append("service_tcp_ordered")
        if self.is_udp_bulk_only:
            signals.append("service_udp_bulk")
        if self.has_mixed_known_classes:
            signals.append("service_mixed_classes")
        if self.protected_degraded:
            signals.append("service_protected_degraded")
        if self.protected_tx_bytes > 0:
            signals.append("service_protected_active")
        if self.bulk_tx_bytes > 0:
            signals.append("service_bulk_active")
        return tuple(signals)

    def export_dict(self) -> dict[str, object]:
        """Return a diagnostic-safe summary without endpoints or secrets."""
        return {
            "protected_services": list(self.protected_services),
            "bulk_services": list(self.bulk_services),
            "unknown_services": list(self.unknown_services),
            "protected_tx_bytes": self.protected_tx_bytes,
            "bulk_tx_bytes": self.bulk_tx_bytes,
            "unknown_tx_bytes": self.unknown_tx_bytes,
            "protected_degraded": list(self.protected_degraded),
            "signals": list(self.signals()),
        }


def service_is_protected(service: ServiceIntentLike) -> bool:
    """Return whether Python should keep this service conservative and ordered."""
    return service.priority in {"high", "critical"} or service.traffic_class in PROTECTED_TRAFFIC_CLASSES


def service_is_bulk(service: ServiceIntentLike) -> bool:
    """Return whether this service should use bulk/throughput path allocation."""
    return service.priority == "bulk" or service.traffic_class in BULK_TRAFFIC_CLASSES


def service_traffic_summary(
    services: Sequence[ServiceIntentLike],
    *,
    service_stats: Mapping[object, object] | None = None,
    service_outcomes: object = None,
) -> ServiceTrafficSummary:
    """
    Summarize configured intent plus optional live service counters.

    Counter values are deliberately coarse cumulative facts. More precise rate
    logic belongs in the budget/path controllers; this summary is for choosing
    safe policy posture and diagnostics.
    """
    service_stats = service_stats or {}
    degraded = _degraded_services(service_outcomes)
    protected: list[str] = []
    bulk: list[str] = []
    unknown: list[str] = []
    protected_tx = 0
    bulk_tx = 0
    unknown_tx = 0
    protected_degraded: list[str] = []
    for service in services:
        tx_bytes = _service_tx_bytes(service.name, service_stats)
        if service_is_protected(service):
            protected.append(service.name)
            protected_tx += tx_bytes
            if service.name in degraded:
                protected_degraded.append(service.name)
        elif service_is_bulk(service):
            bulk.append(service.name)
            bulk_tx += tx_bytes
        else:
            unknown.append(service.name)
            unknown_tx += tx_bytes
    return ServiceTrafficSummary(
        protected_services=tuple(protected),
        bulk_services=tuple(bulk),
        unknown_services=tuple(unknown),
        protected_tx_bytes=protected_tx,
        bulk_tx_bytes=bulk_tx,
        unknown_tx_bytes=unknown_tx,
        protected_degraded=tuple(protected_degraded),
    )


def service_traffic_summary_from_status(
    services: Sequence[ServiceIntentLike],
    status: Mapping[str, object],
) -> ServiceTrafficSummary:
    """Build a summary from the shared runtime status payload."""
    service_stats = status.get("service_stats")
    return service_traffic_summary(
        services,
        service_stats=service_stats if isinstance(service_stats, Mapping) else {},
        service_outcomes=status.get("service_outcomes"),
    )


def _service_tx_bytes(service_name: str, service_stats: Mapping[object, object]) -> int:
    raw = service_stats.get(service_name)
    if not isinstance(raw, Mapping):
        return 0
    try:
        return max(0, int(raw.get("tx_bytes") or 0))
    except (TypeError, ValueError):
        return 0


def _degraded_services(service_outcomes: object) -> set[str]:
    if not isinstance(service_outcomes, Sequence) or isinstance(service_outcomes, (str, bytes)):
        return set()
    degraded: set[str] = set()
    for entry in service_outcomes:
        if not isinstance(entry, Mapping):
            continue
        service = entry.get("service")
        if isinstance(service, str) and bool(entry.get("degraded")):
            degraded.add(service)
    return degraded
