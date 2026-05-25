"""
Python-owned service outcome helpers.

These helpers translate facts known by Python tooling into the compact
``ServiceOutcomeSnapshot`` consumed by the service-budget controller. They do
not change Rust scheduler behavior directly and should not grow dataplane
meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gatherlink.scheduling.service_budget import ServiceOutcomeSignal, ServiceOutcomeSnapshot


@dataclass(frozen=True)
class DualWireGuardOutcomeThresholds:
    """Outcome thresholds for dual-WireGuard benchmark/helper observations."""

    tcp_min_mbit_per_second: float | None = None
    tcp_max_retransmits: int | None = None
    udp_max_loss_percent: float | None = None


def dual_wireguard_outcome_from_results(
    results: list[dict[str, Any]],
    *,
    stable_service: str = "wireguard-stable",
    fast_service: str = "wireguard-fast",
    thresholds: DualWireGuardOutcomeThresholds | None = None,
) -> ServiceOutcomeSnapshot:
    """
    Build service outcomes from dual-WireGuard benchmark/helper result rows.

    TCP/stable and UDP/fast are identified by the benchmark row names because
    this is tooling-side evidence. Runtime service names stay explicit in the
    resulting outcome DTO so the scheduler layer does not learn benchmark
    naming conventions.
    """
    thresholds = thresholds or DualWireGuardOutcomeThresholds()
    signals: list[ServiceOutcomeSignal] = []
    stable_reasons: list[str] = []
    fast_reasons: list[str] = []

    for result in results:
        name = str(result.get("name") or "")
        mbit = _optional_float(result.get("mbit_per_second"))
        if "stable" in name and "tcp" in name:
            retransmits = _optional_int(result.get("retransmits"))
            if thresholds.tcp_min_mbit_per_second is not None and (
                mbit is None or mbit < thresholds.tcp_min_mbit_per_second
            ):
                stable_reasons.append(
                    f"tcp throughput {mbit or 0:.2f} below {thresholds.tcp_min_mbit_per_second:.2f} Mbit/s"
                )
            if thresholds.tcp_max_retransmits is not None and (
                retransmits is None or retransmits > thresholds.tcp_max_retransmits
            ):
                stable_reasons.append(
                    f"tcp retransmits {retransmits if retransmits is not None else 'unknown'} above "
                    f"{thresholds.tcp_max_retransmits}"
                )
        if "fast" in name and "udp" in name:
            loss = _optional_float(result.get("lost_percent"))
            if thresholds.udp_max_loss_percent is not None and (loss is None or loss > thresholds.udp_max_loss_percent):
                fast_reasons.append(
                    f"udp loss {loss if loss is not None else 'unknown'} above "
                    f"{thresholds.udp_max_loss_percent:.2f}%"
                )

    if stable_reasons:
        signals.append(ServiceOutcomeSignal(service=stable_service, degraded=True, reason="; ".join(stable_reasons)))
    if fast_reasons:
        signals.append(ServiceOutcomeSignal(service=fast_service, degraded=True, reason="; ".join(fast_reasons)))
    return ServiceOutcomeSnapshot(tuple(signals))


def outcome_snapshot_to_report(snapshot: ServiceOutcomeSnapshot) -> list[dict[str, object]]:
    """Return a JSON-friendly outcome representation for benchmark reports."""
    return [
        {
            "service": outcome.service,
            "degraded": outcome.degraded,
            "reason": outcome.reason,
        }
        for outcome in snapshot.outcomes
    ]


def service_outcome_snapshot_from_json(raw: object) -> ServiceOutcomeSnapshot | None:
    """
    Parse a JSON-friendly service outcome IPC payload.

    Accepted shapes are deliberately compact:

    - ``{"outcomes": [{"service": "...", "degraded": true, "reason": "..."}]}``
    - ``{"service-name": "reason"}``
    - ``{"service-name": true}``

    Unknown or malformed rows are ignored so helper/operator tooling cannot
    destabilize the runner.
    """
    if isinstance(raw, dict) and isinstance(raw.get("outcomes"), list):
        signals = [_signal_from_mapping(item) for item in raw["outcomes"] if isinstance(item, dict)]
        return ServiceOutcomeSnapshot(tuple(signal for signal in signals if signal is not None))
    if isinstance(raw, dict):
        mapping: dict[str, bool | str] = {}
        for service, value in raw.items():
            if not isinstance(service, str) or service == "outcomes":
                continue
            if isinstance(value, str | bool):
                mapping[service] = value
        return ServiceOutcomeSnapshot.from_mapping(mapping)
    return None


def _signal_from_mapping(item: dict[str, object]) -> ServiceOutcomeSignal | None:
    """Return a service outcome signal from one JSON object."""
    service = item.get("service")
    if not isinstance(service, str) or not service:
        return None
    degraded = bool(item.get("degraded", False))
    reason = item.get("reason")
    return ServiceOutcomeSignal(service=service, degraded=degraded, reason=reason if isinstance(reason, str) else "")


def _optional_float(value: object) -> float | None:
    """Return a finite-ish float value or None for missing/invalid facts."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    """Return an int value or None for missing/invalid facts."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
