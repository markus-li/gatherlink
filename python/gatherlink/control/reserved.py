"""Reserved Gatherlink service dispatch owned by the Python control plane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gatherlink.control import metadata as control_metadata_helpers
from gatherlink.protocol import RESERVED_SERVICE_ID_END, SERVICE_ID_CONTROL_METADATA, decode_control_payload


@dataclass(frozen=True)
class ReservedServicePayload:
    """Python-facing event for one reserved-service payload received by Rust."""

    service_id: int
    path_id: int
    sequence: int
    payload: bytes
    frame_bytes: int


def drain_reserved_service_events(
    dataplane: Any,
    control_metadata: dict[str, object],
    *,
    path_names_by_id: dict[int, str],
    local_targets_by_service_id: dict[int, str],
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
    logger: Callable[[str], None] | None = None,
) -> bool:
    """Decode and apply one control-metadata payload into the shared status shape."""
    control_frame = decode_control_payload(event.payload)
    if control_frame is None:
        _log(logger, f"invalid control metadata payload on path {event.path_id}; dropping {len(event.payload)}B")
        return False

    path_name = path_names_by_id.get(event.path_id, f"path-id:{event.path_id}")
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
        received_at_internal_us=control_metadata_helpers.internal_monotonic_us(),
    )
    mismatches = control_metadata_helpers.verify_service_endpoint_assertions(
        control_metadata,
        local_targets_by_service_id,
    )
    for service_id, reason in mismatches.items():
        _log(logger, f"service endpoint assertion mismatch for service id {service_id}: {reason}")
    return True


def reserved_event_from_py(event: object) -> ReservedServicePayload:
    """Convert a PyO3 reserved-service event into a Python dataclass."""
    return ReservedServicePayload(
        service_id=int(event.service_id()),
        path_id=int(event.path_id()),
        sequence=int(event.sequence()),
        payload=bytes(event.payload()),
        frame_bytes=int(event.frame_bytes()),
    )


def note_control_metadata_sent(
    control_metadata: dict[str, object],
    payload: bytes,
    *,
    frame_bytes: int,
    path_metadata: dict[int, str],
    path_capacity: dict[str, dict[str, int | str | None]],
    path_latency: dict[str, dict[str, int | str | None]] | None = None,
    path_mtu: dict[str, dict[str, int | str | None]] | None = None,
    service_metadata: dict[int, str] | None = None,
    path_name: str | None = None,
    service_endpoint_assertions: dict[int, str] | None = None,
    service_disables: dict[int, str] | None = None,
    service_scheduler_policies: dict[int, tuple[int, int]] | None = None,
    sink_time: list[object] | None = None,
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
            + len(frame.path_mtu)
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
        internal_clock={},
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
