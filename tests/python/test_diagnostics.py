from __future__ import annotations

import asyncio
import json

from gatherlink.diagnostics import DiagnosticEvent, DiagnosticsBus
from gatherlink.diagnostics.bus import drain_diagnostics_until_cancelled
from gatherlink.diagnostics.sinks import JsonlDiagnosticSink


class MemorySink:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    def write(self, event: DiagnosticEvent) -> None:
        self.events.append(event)


class FailingSink:
    def write(self, event: DiagnosticEvent) -> None:
        raise RuntimeError(f"failed {event.code}")


def test_diagnostic_event_factories_export_stable_json() -> None:
    event = DiagnosticEvent.service_bound(service="udp-main", listen="127.0.0.1:55180", target="127.0.0.1:51820")

    payload = event.export_dict()

    assert event.stable_code
    assert payload["code"] == "service.bound"
    assert payload["kind"] == "service_bound"
    assert payload["service"] == "udp-main"
    assert payload["details"] == {"listen": "127.0.0.1:55180", "target": "127.0.0.1:51820"}


def test_helper_event_factory_exports_stable_helper_fact() -> None:
    event = DiagnosticEvent.helper_event(
        code="helper.stream.denied",
        helper="udp_stream",
        severity="warning",
        message="helper UDP stream target denied",
        details={"target_host": "127.0.0.1", "target_port": 8080},
    )

    payload = event.export_dict()

    assert event.stable_code
    assert payload["kind"] == "helper"
    assert payload["helper"] == "udp_stream"
    assert payload["details"] == {"target_host": "127.0.0.1", "target_port": 8080}


def test_runtime_start_failed_factory_exports_stable_error() -> None:
    event = DiagnosticEvent.runtime_start_failed(
        message="cannot start core service",
        details={"error_type": "RustDataplaneUnavailableError"},
    )

    assert event.stable_code
    assert event.code == "runtime.start_failed"
    assert event.kind == "runtime"
    assert event.severity == "error"


def test_diagnostics_bus_drops_oldest_without_blocking() -> None:
    sink = MemorySink()
    bus = DiagnosticsBus(max_queue_size=2, sinks=[sink])

    assert bus.publish(DiagnosticEvent.warning("first"))
    assert bus.publish(DiagnosticEvent.warning("second"))
    assert not bus.publish(DiagnosticEvent.warning("third"))

    assert bus.snapshot()["dropped_events"] == 1
    assert bus.drain() == 2
    assert [event.message for event in sink.events] == ["second", "third"]


def test_diagnostics_bus_isolates_sink_failures() -> None:
    sink = MemorySink()
    bus = DiagnosticsBus(max_queue_size=8, sinks=[FailingSink(), sink])

    bus.publish(DiagnosticEvent.shutdown(reason="test complete"))
    assert bus.drain() == 1

    assert bus.snapshot()["sink_failures"] == 1
    assert sink.events[0].code == "runtime.shutdown"


def test_async_diagnostics_drainer_flushes_until_cancelled() -> None:
    async def scenario() -> None:
        sink = MemorySink()
        bus = DiagnosticsBus(sinks=[sink])
        task = asyncio.create_task(drain_diagnostics_until_cancelled(bus, interval_seconds=0.01, drain_limit=1))
        bus.publish(DiagnosticEvent.warning("async helper event"))
        await asyncio.sleep(0.03)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert [event.message for event in sink.events] == ["async helper event"]

    asyncio.run(scenario())


def test_jsonl_sink_writes_one_event_per_line(tmp_path) -> None:
    output = tmp_path / "diagnostics" / "events.jsonl"
    sink = JsonlDiagnosticSink(output)

    sink.write(DiagnosticEvent.packet_forwarded(service="udp-main", path="path-a", packets=2, bytes_forwarded=64))
    sink.write(DiagnosticEvent.config_reapplied(node="local", generation=7))
    sink.close()

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["code"] for row in rows] == ["packet.forwarded", "config.reapplied"]
    assert rows[0]["details"] == {"packets": 2, "bytes": 64}
    assert rows[1]["details"] == {"generation": 7}
