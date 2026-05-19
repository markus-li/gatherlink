"""
Control metaband cadence policy.

The control metaband is production protocol machinery, not lab behavior. Python
owns the policy for how often to send peer telemetry; Rust should receive the
compiled result and keep packet execution fast.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

BASELINE_CONTROL_ACTIVE_INTERVAL_SECONDS = 5.0
BASELINE_CONTROL_IDLE_INTERVAL_SECONDS = 60.0
MONITOR_CONTROL_ACTIVE_INTERVAL_SECONDS = 1.0
MONITOR_CONTROL_IDLE_INTERVAL_SECONDS = 5.0
MONITOR_CONTROL_REQUEST_TTL_SECONDS = 120.0
MONITOR_CONTROL_REQUEST_REFRESH_SECONDS = 60.0


@dataclass(frozen=True)
class ControlCadenceProfile:
    """
    Named control send cadence for one peer/service pair.

    Baseline cadence sends only often enough to keep services, scheduling, path
    naming, capacity, latency, and clock metadata correct. Monitor cadence is a
    deliberate diagnostic escalation so operator views can become more live
    without making normal tunnels chatty.
    """

    name: str
    active_interval_seconds: float
    idle_interval_seconds: float


BASELINE_CONTROL_CADENCE = ControlCadenceProfile(
    name="baseline",
    active_interval_seconds=BASELINE_CONTROL_ACTIVE_INTERVAL_SECONDS,
    idle_interval_seconds=BASELINE_CONTROL_IDLE_INTERVAL_SECONDS,
)
MONITOR_CONTROL_CADENCE = ControlCadenceProfile(
    name="monitor",
    active_interval_seconds=MONITOR_CONTROL_ACTIVE_INTERVAL_SECONDS,
    idle_interval_seconds=MONITOR_CONTROL_IDLE_INTERVAL_SECONDS,
)


@dataclass
class ControlCadenceState:
    """Small mutable state object tracking whether data traffic moved."""

    profile: ControlCadenceProfile = BASELINE_CONTROL_CADENCE
    previous_traffic_total: int | None = None
    requested_profile: ControlCadenceProfile | None = None
    requested_until_monotonic: float | None = None

    def next_interval(self, current_traffic_total: int) -> float:
        """Return the next interval and remember the traffic total just observed."""
        interval = next_control_interval(
            current_traffic_total,
            self.previous_traffic_total,
            profile=self.effective_profile(),
        )
        self.previous_traffic_total = current_traffic_total
        return interval

    def request_monitor_profile(self, *, ttl_seconds: float = MONITOR_CONTROL_REQUEST_TTL_SECONDS) -> dict[str, object]:
        """Temporarily raise this service to monitor cadence."""
        self.requested_profile = MONITOR_CONTROL_CADENCE
        self.requested_until_monotonic = time.monotonic() + ttl_seconds
        return self.status()

    def effective_profile(self) -> ControlCadenceProfile:
        """Return the active profile, expiring temporary diagnostic requests."""
        if self.requested_profile is None or self.requested_until_monotonic is None:
            return self.profile
        if time.monotonic() >= self.requested_until_monotonic:
            self.requested_profile = None
            self.requested_until_monotonic = None
            return self.profile
        return self.requested_profile

    def status(self) -> dict[str, object]:
        """Return structured cadence state for service status and IPC command replies."""
        profile = self.effective_profile()
        remaining = (
            max(self.requested_until_monotonic - time.monotonic(), 0.0)
            if self.requested_until_monotonic is not None
            else None
        )
        return {
            "profile": profile.name,
            "active_interval_seconds": profile.active_interval_seconds,
            "idle_interval_seconds": profile.idle_interval_seconds,
            "requested_until_seconds": remaining,
        }


def next_control_interval(
    current_traffic_total: int,
    previous_traffic_total: int | None,
    *,
    profile: ControlCadenceProfile = BASELINE_CONTROL_CADENCE,
) -> float:
    """
    Return the next control send interval based on real data movement.

    Startup and active traffic use the profile's active cadence so peers quickly
    exchange the facts needed for service operation and scheduling. Once the
    traffic counters stop changing, the control metaband becomes a sparse
    refresh. Diagnostic tooling can switch to a monitor profile later without
    changing the packet protocol or lab service loops.
    """
    if previous_traffic_total is None or current_traffic_total != previous_traffic_total:
        return profile.active_interval_seconds
    return profile.idle_interval_seconds
