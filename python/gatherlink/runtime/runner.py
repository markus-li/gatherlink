"""
Foreground Rust-backed core service runner.

The runner is Python-owned orchestration around the Rust dataplane. Python loads
expanded runtime state, owns lifecycle and diagnostics, and calls the narrow
Rust handle to move packets. It does not inspect payloads or reimplement packet
scheduling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

from gatherlink.config.runtime import RuntimeConfig
from gatherlink.dataplane.rust_backend import bind_core_dataplane
from gatherlink.runtime.plan import runtime_warnings
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CoreRunnerResult:
    """Summary from a bounded foreground core runner invocation."""

    iterations: int
    forwarded_packets: int
    forwarded_bytes: int
    delivered_packets: int
    delivered_bytes: int


DataplaneFactory = Callable[[RuntimeConfig], Any]


def run_core_service(
    runtime_config: RuntimeConfig,
    *,
    dataplane_factory: DataplaneFactory = bind_core_dataplane,
    stop_event: Event | None = None,
    max_iterations: int | None = None,
    batch_size: int = 32,
) -> CoreRunnerResult:
    """
    Run a Rust-backed core service loop until stopped.

    ``max_iterations`` is a test and smoke-run escape hatch. Normal services
    pass no limit and are stopped through the Python supervisor or process
    signal path.
    """
    service_names = [service.name for service in runtime_config.services if service.listen]
    if not service_names:
        raise ValueError("core runner requires at least one service with a listen address")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    for warning in runtime_warnings(runtime_config):
        logger.warning(warning.removeprefix("WARNING: "))

    dataplane = dataplane_factory(runtime_config)
    stop_event = stop_event or Event()
    iterations = 0
    forwarded_packets = 0
    forwarded_bytes = 0
    delivered_packets = 0
    delivered_bytes = 0

    while not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        for service_name in service_names:
            try:
                outcomes = dataplane.forward_available_for_service(service_name, batch_size)
            except Exception as exc:
                if _is_idle_receive_timeout(exc):
                    continue
                raise
            forwarded_packets += len(outcomes)
            forwarded_bytes += sum(int(outcome.payload_len()) for outcome in outcomes)
        delivered = dataplane.receive_available_from_paths(batch_size)
        delivered_packets += len(delivered)
        delivered_bytes += sum(int(outcome.payload_len()) for outcome in delivered)

    return CoreRunnerResult(
        iterations=iterations,
        forwarded_packets=forwarded_packets,
        forwarded_bytes=forwarded_bytes,
        delivered_packets=delivered_packets,
        delivered_bytes=delivered_bytes,
    )


def _is_idle_receive_timeout(exc: Exception) -> bool:
    """Return whether Rust reported an idle UDP receive timeout."""
    text = str(exc).lower()
    # Rust maps socket idle timeouts through PyO3 as runtime errors. Treating
    # only receive-timeout wording as idle keeps real dataplane errors loud.
    return "failed to receive udp datagram" in text and (
        "timed out" in text or "would block" in text or "resource temporarily unavailable" in text
    )
