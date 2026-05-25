"""Telemetry smoothing owned by the Python scheduler control plane."""

from __future__ import annotations

from dataclasses import dataclass, field

from gatherlink.scheduling.metrics import PathSchedulerMetrics, SchedulerTelemetrySnapshot

SMOOTHING_ALPHA_NUMERATOR = 1
SMOOTHING_ALPHA_DENOMINATOR = 4


@dataclass
class SchedulerTelemetrySmoother:
    """
    Keep small per-path smoothing state across live reapply passes.

    Python owns smoothing and confidence because those are policy decisions.
    Rust should only receive the already-compiled primitive values.
    """

    _previous: dict[str, PathSchedulerMetrics] = field(default_factory=dict)
    _samples: dict[str, int] = field(default_factory=dict)

    def smooth(self, snapshot: SchedulerTelemetrySnapshot) -> SchedulerTelemetrySnapshot:
        """Return a snapshot with noisy scalar telemetry damped by prior samples."""
        smoothed: dict[str, PathSchedulerMetrics] = {}
        for path_name, metrics in snapshot.paths.items():
            previous = self._previous.get(path_name)
            if previous is None:
                smoothed_metrics = metrics
            else:
                smoothed_metrics = metrics.model_copy(
                    update={
                        "tx_capacity_bps": _smooth_optional(previous.tx_capacity_bps, metrics.tx_capacity_bps),
                        "rx_capacity_bps": _smooth_optional(previous.rx_capacity_bps, metrics.rx_capacity_bps),
                        "tx_latency_current_us": _smooth_optional(
                            previous.tx_latency_current_us,
                            metrics.tx_latency_current_us,
                        ),
                        "tx_latency_mean_us": _smooth_optional(previous.tx_latency_mean_us, metrics.tx_latency_mean_us),
                        "rx_latency_current_us": _smooth_optional(
                            previous.rx_latency_current_us,
                            metrics.rx_latency_current_us,
                        ),
                        "rx_latency_mean_us": _smooth_optional(previous.rx_latency_mean_us, metrics.rx_latency_mean_us),
                        "loss_ppm": _smooth_int(previous.loss_ppm, metrics.loss_ppm),
                        "tx_jitter_us": _smooth_optional(previous.tx_jitter_us, metrics.tx_jitter_us),
                        "rx_jitter_us": _smooth_optional(previous.rx_jitter_us, metrics.rx_jitter_us),
                        # Queue, drops, failures, and stale-control age are
                        # immediate pressure facts. Do not smooth them away.
                    }
                )
            smoothed[path_name] = smoothed_metrics
            self._previous[path_name] = smoothed_metrics
            self._samples[path_name] = self._samples.get(path_name, 0) + 1
        stale_paths = set(self._previous) - set(snapshot.paths)
        for path_name in stale_paths:
            self._previous.pop(path_name, None)
            self._samples.pop(path_name, None)
        return SchedulerTelemetrySnapshot(paths=smoothed)

    def confidence(self, path_name: str) -> int:
        """Return the number of samples seen for one path."""
        return self._samples.get(path_name, 0)


def _smooth_optional(previous: int | None, current: int | None) -> int | None:
    """Smooth known optional integer telemetry without inventing missing data."""
    if current is None:
        return previous
    if previous is None:
        return current
    return _smooth_int(previous, current)


def _smooth_int(previous: int, current: int) -> int:
    """Return a small exponential moving average step."""
    return (
        previous * (SMOOTHING_ALPHA_DENOMINATOR - SMOOTHING_ALPHA_NUMERATOR) + current * SMOOTHING_ALPHA_NUMERATOR
    ) // SMOOTHING_ALPHA_DENOMINATOR
