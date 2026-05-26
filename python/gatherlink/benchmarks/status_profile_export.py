"""Convert runtime status snapshots into path profile observations."""

from __future__ import annotations

from typing import Any


def status_observations(
    status: dict[str, Any],
    *,
    duration_seconds: float,
    profile_name: str,
    pressure_mbit: float | None,
) -> dict[str, Any]:
    """Build profile-export observations from Gatherlink service status JSON."""
    path_stats = status.get("path_stats")
    if not isinstance(path_stats, dict) or not path_stats:
        raise ValueError("status JSON must include non-empty path_stats")
    control = status.get("control_metadata") if isinstance(status.get("control_metadata"), dict) else {}
    latency = control.get("path_latency") if isinstance(control.get("path_latency"), dict) else {}
    mtu = control.get("path_mtu") if isinstance(control.get("path_mtu"), dict) else {}

    samples: list[dict[str, Any]] = []
    for path_name in sorted(path_stats):
        stats = path_stats[path_name]
        if not isinstance(stats, dict):
            continue
        bytes_seen = _number(stats.get("tx_bytes")) or _number(stats.get("rx_bytes")) or _number(stats.get("bytes"))
        if bytes_seen is None:
            continue
        path_latency = latency.get(path_name) if isinstance(latency.get(path_name), dict) else {}
        path_mtu = mtu.get(path_name) if isinstance(mtu.get(path_name), dict) else {}
        rtt_us = _number(path_latency.get("rtt_us")) if isinstance(path_latency, dict) else None
        jitter_us = _number(path_latency.get("tx_jitter_us")) if isinstance(path_latency, dict) else None
        samples.append(
            {
                "path": path_name,
                "rx_mbit": round(bytes_seen * 8 / duration_seconds / 1_000_000, 3),
                "rtt_ms": round((rtt_us or 0) / 1000, 3),
                "jitter_ms": round((jitter_us or 0) / 1000, 3),
                "loss_percent": round(
                    (_number(stats.get("missed_packets")) or 0)
                    / max(_number(stats.get("packets")) or 1, 1)
                    * 100,
                    4,
                ),
                "mtu": _mtu_value(path_mtu),
            }
        )
    if not samples:
        raise ValueError("status JSON did not contain usable per-path byte counters")
    payload: dict[str, Any] = {"profile_name": profile_name, "samples": samples}
    if pressure_mbit is not None:
        payload["pressure_mbit"] = pressure_mbit
    return payload


def _number(value: Any) -> float | None:
    """Return numeric telemetry values without accepting booleans as numbers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _mtu_value(path_mtu: dict[str, Any]) -> int | None:
    """Choose the best directional MTU value available in status metadata."""
    for key in ("tx_frame_mtu", "rx_frame_mtu", "frame_mtu", "tx_link_mtu", "rx_link_mtu"):
        value = path_mtu.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None
