"""
Bounded diagnostic event bus with isolated sinks.

Publishing is intentionally cheap and non-blocking: producers append to a
bounded in-memory queue and return. Control loops can call ``drain`` from a
safe place to fan events out to JSONL or other sinks. If the queue is full, the
oldest diagnostic is dropped and a local counter is incremented; dataplane and
control behavior must keep moving.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Protocol

from gatherlink.diagnostics.events import DiagnosticEvent
from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


class DiagnosticSink(Protocol):
    """Synchronous sink interface used by the first diagnostics bus."""

    def write(self, event: DiagnosticEvent) -> None:
        """Persist or render one diagnostic event."""


class DiagnosticsBus:
    """Small bounded queue that isolates diagnostics producers from sinks."""

    def __init__(self, *, max_queue_size: int = 4096, sinks: Iterable[DiagnosticSink] = ()) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        self._events: deque[DiagnosticEvent] = deque(maxlen=max_queue_size)
        self._sinks: list[DiagnosticSink] = list(sinks)
        self.dropped_events = 0
        self.sink_failures = 0

    @property
    def queued_events(self) -> int:
        """Return the number of events waiting for sink delivery."""
        return len(self._events)

    @property
    def sinks(self) -> tuple[DiagnosticSink, ...]:
        """Return currently registered sinks."""
        return tuple(self._sinks)

    def add_sink(self, sink: DiagnosticSink) -> None:
        """Register another sink without disturbing existing queued events."""
        self._sinks.append(sink)

    def publish(self, event: DiagnosticEvent) -> bool:
        """
        Queue an event without waiting for sinks.

        Returns ``False`` when the queue was already full and the oldest queued
        event had to be discarded to make room. The new event is still accepted
        so operators see the most recent facts during a burst.
        """
        accepted_without_drop = len(self._events) < self._events.maxlen
        if not accepted_without_drop:
            self.dropped_events += 1
        self._events.append(event)
        return accepted_without_drop

    def publish_warning(self, message: str, **kwargs: object) -> bool:
        """Convenience wrapper for the common warning path."""
        return self.publish(DiagnosticEvent.warning(message, **kwargs))

    def drain(self, *, limit: int | None = None) -> int:
        """
        Deliver queued events to registered sinks.

        Sink exceptions are logged and counted, but they do not stop delivery to
        the remaining sinks and do not re-raise into service loops.
        """
        delivered = 0
        while self._events and (limit is None or delivered < limit):
            event = self._events.popleft()
            for sink in self._sinks:
                try:
                    sink.write(event)
                except Exception as exc:  # pragma: no cover - exact sink failures vary by platform.
                    self.sink_failures += 1
                    logger.warning("diagnostic sink failed", extra={"event_code": event.code, "error": str(exc)})
            delivered += 1
        return delivered

    def snapshot(self) -> dict[str, int]:
        """Return counters useful for service status and tests."""
        return {
            "queued_events": self.queued_events,
            "dropped_events": self.dropped_events,
            "sink_failures": self.sink_failures,
            "sink_count": len(self._sinks),
        }


async def drain_diagnostics_until_cancelled(
    bus: DiagnosticsBus,
    *,
    interval_seconds: float = 0.25,
    drain_limit: int = 256,
) -> None:
    """
    Periodically flush diagnostics from async foreground services.

    Producers still only call ``publish``. This small control-plane task runs
    beside helper services and runtime loops, keeping file/network sinks away
    from packet and stream handling paths. Cancellation performs one final
    drain so shutdown events are not left in memory.
    """
    try:
        while True:
            bus.drain(limit=drain_limit)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        bus.drain()
        raise


@contextmanager
def drain_diagnostics_in_background(
    bus: DiagnosticsBus | None,
    *,
    interval_seconds: float = 0.25,
    drain_limit: int = 256,
) -> Iterator[None]:
    """
    Periodically flush diagnostics for synchronous foreground services.

    Some helper libraries expose a blocking ``run`` method rather than an async
    service loop. This context keeps those helpers on the same non-blocking
    diagnostics model as async services: producers enqueue structured facts,
    and a tiny control-plane thread performs best-effort sink delivery away
    from stream handling.
    """
    if bus is None:
        yield
        return

    stop_event = threading.Event()

    def drain_loop() -> None:
        while not stop_event.wait(interval_seconds):
            bus.drain(limit=drain_limit)

    thread = threading.Thread(target=drain_loop, name="gatherlink-diagnostics-drain", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=max(interval_seconds * 2, 0.5))
        bus.drain()
