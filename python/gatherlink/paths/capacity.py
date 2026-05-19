"""Path capacity detection helpers owned by Python path telemetry."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

PATH_CAPACITY_DETECTION_WINDOW_SECONDS = 5.0
PATH_CAPACITY_INCREASE_SUSTAIN_SECONDS = 15.0
PATH_CAPACITY_DECREASE_SUSTAIN_SECONDS = 60.0
PATH_CAPACITY_MIN_CHANGE_RATIO = 0.15
PATH_CAPACITY_HEADROOM_RATIO = 1.05
PATH_CAPACITY_ADJUSTMENT_RATIO = 0.25
PATH_CAPACITY_MIN_SAMPLE_BYTES = 16 * 1024
PATH_CAPACITY_DEFAULT_BPS = 50_000_000


@dataclass(frozen=True)
class PathCapacityDefaults:
    """Initial path capacity estimates for one local direction."""

    path_name: str
    tx_bps: int | None = None
    rx_bps: int | None = None
    source: str = "config"
    updated_at: str | None = None

    def export_dict(self) -> dict[str, int | str | None]:
        """Return the control-metadata representation used by schedulers and monitors."""
        return {
            "tx_bps": self.tx_bps,
            "rx_bps": self.rx_bps,
            "source": self.source,
            "updated_at": self.updated_at,
        }


class PathCapacityDetector:
    """
    Detect sustained directional path capacity changes from path samples.

    Lab code can feed qdisc-shaped samples, while production services can feed
    dataplane/path telemetry. The rules live here so scheduler input is
    interpreted the same way no matter where the sample came from.
    """

    def __init__(
        self,
        *,
        path_names: list[str],
        direction: str,
        initial_estimates: dict[str, dict[str, int | str | None]],
    ) -> None:
        if direction not in {"tx", "rx"}:
            raise ValueError("capacity direction must be 'tx' or 'rx'")
        self._path_names = path_names
        self._direction = direction
        self._estimates = {
            path_name: _filled_estimate(initial_estimates.get(path_name, {}), direction)
            for path_name in path_names
        }
        self._dirty = set(path_names)
        self._last_sample_at = time.monotonic()
        self._last_bytes = {path_name: 0 for path_name in path_names}
        self._last_payload_bytes = {path_name: 0 for path_name in path_names}
        self._last_drops = {path_name: 0 for path_name in path_names}
        self._sustained = {path_name: _empty_capacity_observation() for path_name in path_names}

    def snapshot(self) -> dict[str, dict[str, int | str | None]]:
        """Return all current capacity estimates."""
        return {path_name: dict(self._estimates[path_name]) for path_name in self._path_names}

    def dirty_snapshot(self) -> dict[str, dict[str, int | str | None]]:
        """Return capacity estimates that changed since the last send mark."""
        return {
            path_name: dict(self._estimates[path_name]) for path_name in self._path_names if path_name in self._dirty
        }

    def mark_sent(self) -> None:
        """Mark current estimates as advertised."""
        self._dirty.clear()

    def observe(
        self,
        path_stats: dict[str, dict[str, int]],
        sample_stats: dict[str, dict[str, int]],
    ) -> dict[str, dict[str, int | str | None]]:
        """Observe cumulative path samples and return estimates that changed."""
        now = time.monotonic()
        elapsed = now - self._last_sample_at
        if elapsed < PATH_CAPACITY_DETECTION_WINDOW_SECONDS:
            return {}

        changed: dict[str, dict[str, int | str | None]] = {}
        for path_name in self._path_names:
            sample_rate_bps = _int_or_none(sample_stats.get(path_name, {}).get("rate_bps"))
            capacity_key = f"{self._direction}_bps"
            current_bps = int(self._estimates[path_name].get(capacity_key) or PATH_CAPACITY_DEFAULT_BPS)
            current_bytes = _sample_bytes(path_name, path_stats, sample_stats)
            current_payload_bytes = path_stats.get(path_name, {}).get("bytes", 0)
            current_drops = sample_stats.get(path_name, {}).get("dropped", 0)
            delta_bytes = max(current_bytes - self._last_bytes.get(path_name, 0), 0)
            delta_payload_bytes = max(current_payload_bytes - self._last_payload_bytes.get(path_name, 0), 0)
            delta_drops = max(current_drops - self._last_drops.get(path_name, 0), 0)
            self._last_bytes[path_name] = current_bytes
            self._last_payload_bytes[path_name] = current_payload_bytes
            self._last_drops[path_name] = current_drops

            if delta_bytes < PATH_CAPACITY_MIN_SAMPLE_BYTES or delta_payload_bytes < PATH_CAPACITY_MIN_SAMPLE_BYTES:
                self._reset_sustained(path_name)
                continue

            observed_bps = max(int((delta_bytes * 8) / elapsed), 1)
            sample_bps = sample_rate_bps if sample_rate_bps is not None and sample_rate_bps > current_bps else observed_bps
            candidate_bps = self._sustained_candidate(
                path_name, current_bps, sample_bps, delta_bytes, elapsed, delta_drops
            )

            if candidate_bps is None or not _capacity_changed(current_bps, candidate_bps):
                continue

            self._estimates[path_name][capacity_key] = _step_capacity(current_bps, candidate_bps)
            self._estimates[path_name]["source"] = "detected"
            self._estimates[path_name]["updated_at"] = datetime.now(UTC).isoformat()
            self._dirty.add(path_name)
            changed[path_name] = dict(self._estimates[path_name])
            self._reset_sustained(path_name)

        self._last_sample_at = now
        return changed

    def _sustained_candidate(
        self,
        path_name: str,
        current_bps: int,
        sample_bps: int,
        sample_bytes: int,
        elapsed: float,
        dropped_packets: int,
    ) -> int | None:
        direction = _capacity_sample_direction(current_bps, sample_bps, dropped_packets)
        if direction is None:
            self._reset_sustained(path_name)
            return None

        sustained = self._sustained[path_name]
        if sustained["direction"] != direction:
            sustained.update(_empty_capacity_observation(direction=direction))

        sustained["seconds"] = float(sustained["seconds"]) + elapsed
        sustained["bytes"] = int(sustained["bytes"]) + sample_bytes
        sustained["drops"] = int(sustained["drops"]) + dropped_packets

        required_seconds = (
            PATH_CAPACITY_INCREASE_SUSTAIN_SECONDS
            if direction == "increase"
            else PATH_CAPACITY_DECREASE_SUSTAIN_SECONDS
        )
        if float(sustained["seconds"]) < required_seconds:
            return None
        if direction == "decrease" and int(sustained["drops"]) <= 0:
            return None

        average_bps = int((int(sustained["bytes"]) * 8) / max(float(sustained["seconds"]), 0.001))
        return int(average_bps * PATH_CAPACITY_HEADROOM_RATIO)

    def _reset_sustained(self, path_name: str) -> None:
        self._sustained[path_name] = _empty_capacity_observation()


def _filled_estimate(
    estimate: dict[str, int | str | None],
    direction: str,
) -> dict[str, int | str | None]:
    filled = {
        "tx_bps": _int_or_none(estimate.get("tx_bps")),
        "rx_bps": _int_or_none(estimate.get("rx_bps")),
        "source": str(estimate.get("source") or "config"),
        "updated_at": estimate.get("updated_at") if isinstance(estimate.get("updated_at"), str) else None,
    }
    if filled[f"{direction}_bps"] is None:
        filled[f"{direction}_bps"] = PATH_CAPACITY_DEFAULT_BPS
    return filled


def _sample_bytes(
    path_name: str,
    path_stats: dict[str, dict[str, int]],
    sample_stats: dict[str, dict[str, int]],
) -> int:
    sample_row = sample_stats.get(path_name)
    if sample_row is not None and "sent_bytes" in sample_row:
        return sample_row["sent_bytes"]
    return path_stats.get(path_name, {}).get("bytes", 0)


def _empty_capacity_observation(*, direction: str | None = None) -> dict[str, float | int | str | None]:
    return {"direction": direction, "seconds": 0.0, "bytes": 0, "drops": 0}


def _capacity_sample_direction(current_bps: int, sample_bps: int, dropped_packets: int) -> str | None:
    if sample_bps > current_bps * (1 + PATH_CAPACITY_MIN_CHANGE_RATIO):
        return "increase"
    if dropped_packets > 0 and sample_bps < current_bps * (1 - PATH_CAPACITY_MIN_CHANGE_RATIO):
        return "decrease"
    return None


def _step_capacity(current_bps: int, candidate_bps: int) -> int:
    delta = candidate_bps - current_bps
    if delta == 0:
        return current_bps
    stepped = int(current_bps + (delta * PATH_CAPACITY_ADJUSTMENT_RATIO))
    if delta > 0:
        return max(current_bps + 1, min(stepped, candidate_bps))
    return min(current_bps - 1, max(stepped, candidate_bps))


def _capacity_changed(current_bps: int, candidate_bps: int) -> bool:
    if current_bps <= 0:
        return True
    return abs(candidate_bps - current_bps) / current_bps >= PATH_CAPACITY_MIN_CHANGE_RATIO


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
