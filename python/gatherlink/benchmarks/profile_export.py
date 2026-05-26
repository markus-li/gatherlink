"""Export observed path behavior into repeatable benchmark profile drafts."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PathObservation:
    """One observed path sample from a benchmark or profiler run."""

    path: str
    rx_mbit: float
    rtt_ms: float
    jitter_ms: float = 0.0
    loss_percent: float = 0.0
    mtu: int | None = None


@dataclass(frozen=True)
class ExportedProfile:
    """A thresholds-style benchmark profile draft."""

    name: str
    expected_capacity_mbit: float
    path_capacity_mbit: tuple[float, ...]
    pressure_mbit: float
    path_mtu: int
    payload_size: int
    network_mode: dict[str, Any]

    def export_dict(self) -> dict[str, Any]:
        """Return a stable JSON representation for docs or thresholds files."""
        return {
            "name": self.name,
            "expected_capacity_mbit": self.expected_capacity_mbit,
            "path_capacity_mbit": list(self.path_capacity_mbit),
            "pressure_mbit": self.pressure_mbit,
            "path_mtu": self.path_mtu,
            "payload_size": self.payload_size,
            "network_mode": self.network_mode,
        }


def load_observations(path: Path) -> tuple[str, list[PathObservation], float | None]:
    """Load path observations from a benchmark/profiler JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    name = str(raw.get("profile_name") or raw.get("name") or path.stem)
    pressure = raw.get("pressure_mbit")
    samples = raw.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("profile observation file must contain non-empty samples")
    observations = [_observation_from_raw(item) for item in samples]
    return name, observations, float(pressure) if pressure is not None else None


def export_profile(
    name: str,
    observations: Iterable[PathObservation],
    *,
    pressure_mbit: float | None = None,
    payload_size: int | None = None,
) -> ExportedProfile:
    """
    Build a repeatable lab profile draft from observed path samples.

    The exporter intentionally uses conservative medians rather than maxima.
    Operators can hand-edit the result, but the first generated profile should
    not overpromise a volatile link just because one sample was fast.
    """
    grouped: dict[str, list[PathObservation]] = defaultdict(list)
    for observation in observations:
        if observation.rx_mbit < 0:
            raise ValueError("rx_mbit must be non-negative")
        grouped[observation.path].append(observation)
    if not grouped:
        raise ValueError("at least one path observation is required")

    ordered_paths = sorted(grouped)
    path_capacity = tuple(round(_median(item.rx_mbit for item in grouped[path]), 2) for path in ordered_paths)
    expected_capacity = round(sum(path_capacity), 2)
    inferred_mtu = _infer_profile_mtu(grouped.values())
    inferred_payload = payload_size if payload_size is not None else min(inferred_mtu, 1200)
    inferred_pressure = pressure_mbit if pressure_mbit is not None else round(expected_capacity * 1.03, 2)
    network_mode = {
        "description": "Generated from observed path samples; review before using as release evidence.",
        "targets": [
            {
                "path": path,
                "shape": {
                    "rate": f"{path_capacity[index]:g}mbit",
                    "delay": f"{round(_median(item.rtt_ms for item in grouped[path]) / 2, 2):g}ms",
                    "jitter": f"{round(_median(item.jitter_ms for item in grouped[path]), 2):g}ms",
                    "loss": f"{round(_median(item.loss_percent for item in grouped[path]), 3):g}%",
                },
            }
            for index, path in enumerate(ordered_paths)
        ],
    }
    return ExportedProfile(
        name=name,
        expected_capacity_mbit=expected_capacity,
        path_capacity_mbit=path_capacity,
        pressure_mbit=round(inferred_pressure, 2),
        path_mtu=inferred_mtu,
        payload_size=inferred_payload,
        network_mode=network_mode,
    )


def _observation_from_raw(raw: Any) -> PathObservation:
    """Convert one JSON object into a typed observation."""
    if not isinstance(raw, dict):
        raise ValueError("each sample must be an object")
    return PathObservation(
        path=str(raw["path"]),
        rx_mbit=float(raw["rx_mbit"]),
        rtt_ms=float(raw.get("rtt_ms", raw.get("latency_ms", 0.0))),
        jitter_ms=float(raw.get("jitter_ms", 0.0)),
        loss_percent=float(raw.get("loss_percent", raw.get("loss", 0.0))),
        mtu=int(raw["mtu"]) if raw.get("mtu") is not None else None,
    )


def _median(values: Iterable[float]) -> float:
    """Return the median of a non-empty numeric sequence."""
    materialized = list(values)
    if not materialized:
        raise ValueError("median requires at least one value")
    return float(statistics.median(materialized))


def _infer_profile_mtu(groups: Iterable[list[PathObservation]]) -> int:
    """Choose a conservative profile MTU from observed path samples."""
    mtus = [item.mtu for group in groups for item in group if item.mtu is not None]
    if not mtus:
        return 1200
    return min(mtus)
