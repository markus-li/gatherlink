"""Reserved Gatherlink service dispatch owned by the Python control plane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gatherlink.control import metadata as control_metadata_helpers
from gatherlink.paths.telemetry import DataTrafficLatencyTracker, PathLatencyTracker
from gatherlink.protocol import RESERVED_SERVICE_ID_END, SERVICE_ID_CONTROL_METADATA, decode_control_payload
from gatherlink.time.offset import InternalClockSyncClient, InternalClockSyncMessage


@dataclass(frozen=True)
class ReservedServicePayload:
    """Python-facing event for one reserved-service payload received by Rust."""

    service_id: int
    path_id: int
    sequence: int
    payload: bytes
    frame_bytes: int
    peer_scope: int | None = None


def drain_reserved_service_events(
    dataplane: Any,
    control_metadata: dict[str, object],
    *,
    path_names_by_id: dict[int, str],
    local_targets_by_service_id: dict[int, str],
    clock_sync_client: InternalClockSyncClient | None = None,
    clock_sync_responses: list[InternalClockSyncMessage] | None = None,
    path_latency_tracker: PathLatencyTracker | None = None,
    data_traffic_latency_tracker: DataTrafficLatencyTracker | None = None,
    local_clock_is_authoritative: bool = False,
    extra_handlers: dict[int, Callable[[ReservedServicePayload], bool]] | None = None,
    logger: Callable[[str], None] | None = None,
) -> int:
    """
    Drain reserved-service payloads from Rust and dispatch the ones Python understands.

    Rust only identifies the reserved service id and preserves the payload bytes.
    This dispatcher is the extension point for control metadata today and later
    remote status, internal DNS, config apply, auth, and multi-hop control
    services. Unknown reserved ids are loud but non-fatal so new service ids can
    be added in Python without Rust changes.
    """
    handled = 0
    handlers = extra_handlers or {}
    for raw_event in dataplane.drain_reserved_service_events():
        event = reserved_event_from_py(raw_event)
        if event.service_id > RESERVED_SERVICE_ID_END:
            _log(
                logger,
                f"non-reserved service id {event.service_id} reached Python reserved dispatcher; "
                f"dropping {len(event.payload)}B",
            )
            continue
        if event.service_id in handlers:
            if handlers[event.service_id](event):
                handled += 1
            continue
        if event.service_id == SERVICE_ID_CONTROL_METADATA:
            if handle_control_metadata_event(
                event,
                control_metadata,
                path_names_by_id=path_names_by_id,
                local_targets_by_service_id=local_targets_by_service_id,
                clock_sync_client=clock_sync_client,
                clock_sync_responses=clock_sync_responses,
                path_latency_tracker=path_latency_tracker,
                data_traffic_latency_tracker=data_traffic_latency_tracker,
                local_clock_is_authoritative=local_clock_is_authoritative,
                logger=logger,
            ):
                handled += 1
            continue
        _log(logger, f"reserved service id {event.service_id} has no Python decoder; dropping {len(event.payload)}B")
    return handled


def handle_control_metadata_event(
    event: ReservedServicePayload,
    control_metadata: dict[str, object],
    *,
    path_names_by_id: dict[int, str],
    local_targets_by_service_id: dict[int, str],
    clock_sync_client: InternalClockSyncClient | None = None,
    clock_sync_responses: list[InternalClockSyncMessage] | None = None,
    path_latency_tracker: PathLatencyTracker | None = None,
    data_traffic_latency_tracker: DataTrafficLatencyTracker | None = None,
    local_clock_is_authoritative: bool = False,
    logger: Callable[[str], None] | None = None,
) -> bool:
    """Decode and apply one control-metadata payload into the shared status shape."""
    _ = local_targets_by_service_id
    control_frame = decode_control_payload(event.payload)
    if control_frame is None:
        _log(logger, f"invalid control metadata payload on path {event.path_id}; dropping {len(event.payload)}B")
        return False

    path_name = path_names_by_id.get(event.path_id, f"path-id:{event.path_id}")
    received_at_internal_us = control_metadata_helpers.internal_monotonic_us()
    control_metadata_helpers.record_control_metadata_received(
        control_metadata,
        event.frame_bytes,
        control_frame,
        f"reserved-service:{event.service_id}",
        path_name=path_name,
    )
    control_metadata_helpers.record_control_path_capacity(
        control_metadata,
        control_frame.path_capacity_bps,
        control_frame.path_metadata,
        path_names_by_id,
    )
    control_metadata_helpers.record_control_path_latency(
        control_metadata,
        control_frame.path_latency_us,
        control_frame.path_metadata,
        path_names_by_id,
    )
    control_metadata_helpers.record_control_path_latency_quality(
        control_metadata,
        control_frame.path_latency_quality,
        control_frame.path_metadata,
        path_names_by_id,
    )
    control_metadata_helpers.record_control_path_latency_stats(
        control_metadata,
        control_frame.path_latency_stats,
        control_frame.path_metadata,
        path_names_by_id,
    )
    control_metadata_helpers.record_control_path_mtu(
        control_metadata,
        control_frame.path_mtu,
        control_frame.path_metadata,
        path_names_by_id,
    )
    control_metadata_helpers.record_sink_time(
        control_metadata,
        control_frame.sink_time,
        path_names_by_id,
        received_at_internal_us=received_at_internal_us,
    )
    if clock_sync_client is not None:
        clock_update = clock_sync_client.observe_control_frame(
            control_frame.internal_clock_sync,
            path_names_by_id=path_names_by_id,
        )
        if path_latency_tracker is not None:
            _merge_clock_sync_latency(control_metadata, path_latency_tracker, clock_update)
        control_metadata_helpers.merge_internal_clock_sync(
            control_metadata,
            _clock_update_without_latency_events(clock_update),
        )
    if clock_sync_responses is not None:
        clock_sync_responses.extend(
            control_metadata_helpers.sink_clock_sync_responses(
                control_frame.internal_clock_sync,
                received_at_us=received_at_internal_us,
            )
        )
    if data_traffic_latency_tracker is not None and path_latency_tracker is not None:
        changed = data_traffic_latency_tracker.observe_peer_transmit_samples(
            control_frame.data_transmit_samples,
            peer_scope=event.peer_scope,
            local_clock_offset_us=_current_clock_offset_us(control_metadata),
            local_clock_is_authoritative=local_clock_is_authoritative,
            latency_tracker=path_latency_tracker,
            rtt_us=_current_clock_rtt_us(control_metadata),
            clock_error_us=_current_clock_error_us(control_metadata),
        )
        if changed:
            control_metadata_helpers.merge_control_path_latency(control_metadata, changed)
    return True


def _merge_clock_sync_latency(
    control_metadata: dict[str, object],
    path_latency_tracker: PathLatencyTracker,
    clock_update: dict[str, object],
) -> None:
    """Promote accepted/rejected path-pinned clock samples into scheduler-visible latency."""
    changed: dict[str, dict[str, int | str | None]] = {}
    observations = clock_update.get("path_latency_observations")
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            path_name = observation.get("path")
            if not isinstance(path_name, str):
                continue
            changed.update(
                path_latency_tracker.observe_directional(
                    path_name,
                    tx_one_way_us=_optional_int(observation.get("tx_one_way_us")),
                    rx_one_way_us=_optional_int(observation.get("rx_one_way_us")),
                    source=str(observation.get("source") or "clock-synced-one-way"),
                    confidence=str(observation.get("confidence") or "warming"),
                    rtt_us=_optional_int(observation.get("rtt_us")),
                    clock_error_us=_optional_int(observation.get("clock_error_us")),
                )
            )
    rejections = clock_update.get("path_latency_rejections")
    if isinstance(rejections, list):
        for rejection in rejections:
            if not isinstance(rejection, dict):
                continue
            path_name = rejection.get("path")
            if not isinstance(path_name, str):
                continue
            changed.update(
                path_latency_tracker.reject(
                    path_name,
                    str(rejection.get("reason") or "rejected"),
                    rtt_us=_optional_int(rejection.get("rtt_us")),
                    clock_error_us=_optional_int(rejection.get("clock_error_us")),
                )
            )
    if changed:
        control_metadata_helpers.merge_control_path_latency(control_metadata, changed)


def _clock_update_without_latency_events(clock_update: dict[str, object]) -> dict[str, object]:
    """Keep path-latency event lists out of the compact internal-clock status row."""
    return {
        key: value
        for key, value in clock_update.items()
        if key not in {"path_latency_observations", "path_latency_rejections"}
    }


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _current_clock_offset_us(control_metadata: dict[str, object]) -> int | None:
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return None
    return _optional_int(internal_clock.get("mean_offset_us") or internal_clock.get("offset_us"))


def _current_clock_rtt_us(control_metadata: dict[str, object]) -> int | None:
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return None
    return _optional_int(internal_clock.get("mean_rtt_us") or internal_clock.get("rtt_us"))


def _current_clock_error_us(control_metadata: dict[str, object]) -> int | None:
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return None
    return _optional_int(internal_clock.get("error_budget_us"))


def reserved_event_from_py(event: object) -> ReservedServicePayload:
    """Convert a PyO3 reserved-service event into a Python dataclass."""
    return ReservedServicePayload(
        service_id=int(event.service_id()),
        path_id=int(event.path_id()),
        sequence=int(event.sequence()),
        payload=bytes(event.payload()),
        frame_bytes=int(event.frame_bytes()),
        peer_scope=_event_peer_scope(event),
    )


def _event_peer_scope(event: object) -> int | None:
    peer_scope = getattr(event, "peer_scope", None)
    if not callable(peer_scope):
        return None
    value = peer_scope()
    return int(value) if value is not None else None


def note_control_metadata_sent(
    control_metadata: dict[str, object],
    payload: bytes,
    *,
    frame_bytes: int,
    path_metadata: dict[int, str],
    path_capacity: dict[str, dict[str, int | str | None]],
    path_latency: dict[str, dict[str, int | str | None]] | None = None,
    path_mtu: dict[str, dict[str, int | str | None]] | None = None,
    path_pressure: dict[str, dict[str, int | str | None]] | None = None,
    service_metadata: dict[int, str] | None = None,
    path_name: str | None = None,
    service_endpoint_assertions: dict[int, str] | None = None,
    service_disables: dict[int, str] | None = None,
    service_scheduler_policies: dict[int, tuple[int, int, int, int, int, int]] | None = None,
    sink_time: list[object] | None = None,
    internal_clock: dict[str, object] | None = None,
) -> None:
    """Record locally-sent control metadata after Python asked Rust to frame it."""
    frame = decode_control_payload(payload)
    message_count = 0
    if frame is not None:
        message_count = (
            len(frame.path_metadata)
            + len(frame.service_metadata)
            + len(frame.service_endpoint_assertions)
            + len(frame.service_disables)
            + len(frame.service_scheduler_policies)
            + len(frame.path_capacity_bps)
            + len(frame.path_latency_us)
            + len(frame.path_latency_quality)
            + len(frame.path_latency_stats)
            + len(frame.path_mtu)
            + len(frame.path_pressure)
            + (1 if frame.scheduler_status is not None else 0)
            + len(frame.data_transmit_samples)
            + len(frame.internal_clock_sync)
            + len(frame.sink_time)
        )
    control_metadata_helpers.record_control_metadata_sent(
        control_metadata,
        frame_bytes,
        message_count=message_count,
        path_metadata=path_metadata,
        path_capacity=path_capacity,
        path_latency=path_latency or {},
        path_mtu=path_mtu,
        path_pressure=path_pressure or {},
        internal_clock=internal_clock or {},
        sink_time=sink_time or [],
        path_name=path_name,
    )
    if service_metadata:
        control_metadata_helpers.merge_control_service_metadata(control_metadata, service_metadata)
    if service_endpoint_assertions:
        control_metadata_helpers.merge_control_service_endpoint_assertions(
            control_metadata,
            service_endpoint_assertions,
        )
    if service_disables:
        control_metadata_helpers.merge_control_service_disables(control_metadata, service_disables)
    if service_scheduler_policies:
        control_metadata_helpers.merge_control_service_scheduler_policies(control_metadata, service_scheduler_policies)
    sent = control_metadata.get("sent")
    if isinstance(sent, dict):
        sent["last_at"] = datetime.now(UTC).isoformat()


def _log(logger: Callable[[str], None] | None, message: str) -> None:
    if logger is None:
        print(message, flush=True)
    else:
        logger(message)
