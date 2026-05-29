"""Outbound control metadata announcements owned by the Python control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gatherlink.control import metadata as control_metadata_helpers
from gatherlink.control import reserved
from gatherlink.protocol import (
    GATHERLINK_V1_HEADER_LEN,
    SERVICE_ID_CONTROL_METADATA,
    DataTransmitSample,
    encode_control_payload,
)
from gatherlink.time.offset import InternalClockSyncMessage


@dataclass(frozen=True)
class ControlMetadataAnnouncement:
    """Summary of one local control-metadata payload handed to Rust."""

    sent_paths: int
    path_count: int
    service_count: int
    endpoint_assertion_count: int
    scheduler_policy_count: int
    service_disable_count: int
    capacity_count: int
    latency_count: int
    mtu_count: int
    pressure_count: int
    data_transmit_sample_count: int
    sink_time_count: int
    clock_sync_count: int
    omitted_data_transmit_sample_count: int
    payload_bytes: int


@dataclass(frozen=True)
class PathPinnedControlAnnouncement:
    """Summary of path-pinned control payloads handed to Rust."""

    sent_paths: int
    clock_sync_count: int
    payload_bytes: int


def announce_control_metadata(
    dataplane: Any,
    runtime_config: Any,
    control_metadata: dict[str, object],
    *,
    path_capacity: dict[str, dict[str, int | str | None]] | None = None,
    path_latency: dict[str, dict[str, int | str | None]] | None = None,
    path_mtu: dict[str, dict[str, int | str | None]] | None = None,
    path_pressure: dict[str, dict[str, int | str | None]] | None = None,
    scheduler_status: tuple[str, str, str] | None = None,
    data_transmit_samples: list[DataTransmitSample] | None = None,
    service_disables: dict[int, str] | None = None,
    path_clock_sync: list[InternalClockSyncMessage] | None = None,
    ntp_sample: Any | None = None,
    include_sink_time: bool = False,
) -> ControlMetadataAnnouncement:
    """
    Build sparse peer metadata and ask Rust to transmit it as a reserved service.

    This is production control-plane behavior, not lab behavior. Python decides
    which facts should be advertised, keeps the status counters coherent, and
    gives Rust an ordinary reserved-service payload to schedule using the
    already-applied service policy.
    """
    path_ids = {path.name: path.scheduler.path_id for path in runtime_config.paths}
    path_metadata = {path.scheduler.path_id: path.name for path in runtime_config.paths}
    service_metadata = {service.service_id: service.name for service in runtime_config.services}
    service_endpoint_assertions = {service.service_id: service.target for service in runtime_config.services}
    service_scheduler_policies = {
        service.service_id: (
            service.scheduler_fanout,
            service.scheduler_fanout_below_bytes,
            service.scheduler_flowlet_idle_us,
            service.scheduler_flowlet_max_hold_us,
            service.scheduler_path_run_datagrams,
            _service_path_policy_code(service.scheduler_path_policy),
        )
        for service in runtime_config.services
    }
    sink_time = (
        list(control_metadata_helpers.sink_time_messages(list(path_ids), path_ids, ntp_sample).values())
        if include_sink_time
        else []
    )
    path_capacity = path_capacity or {}
    path_latency = path_latency or {}
    path_mtu = path_mtu or {}
    path_pressure = path_pressure or {}
    original_data_transmit_sample_count = len(data_transmit_samples or [])
    data_transmit_samples = data_transmit_samples or []
    service_disables = service_disables or {}
    path_clock_sync = path_clock_sync or []
    payload_kwargs = {
        "service_metadata": service_metadata,
        "service_endpoint_assertions": service_endpoint_assertions,
        "service_scheduler_policies": service_scheduler_policies,
        "service_disables": service_disables,
        "path_capacity_bps": control_metadata_helpers.capacity_by_path_id(path_capacity, path_ids),
        "path_latency_us": control_metadata_helpers.latency_by_path_id(path_latency, path_ids),
        "path_latency_quality": control_metadata_helpers.latency_quality_by_path_id(path_latency, path_ids),
        "path_latency_stats": control_metadata_helpers.latency_stats_by_path_id(path_latency, path_ids),
        "path_mtu": control_metadata_helpers.mtu_by_path_id(path_mtu, path_ids),
        "path_pressure": control_metadata_helpers.pressure_by_path_id(path_pressure, path_ids),
        "scheduler_status": scheduler_status,
        "path_clock_sync": path_clock_sync,
        "sink_time": sink_time,
    }
    payload, data_transmit_samples = _encode_control_payload_with_path_budget(
        runtime_config,
        path_metadata,
        data_transmit_samples,
        payload_kwargs,
    )
    if scheduler_status is not None:
        configured_mode, effective_mode, rust_mode = scheduler_status
        control_metadata_helpers.set_local_scheduler_status(
            control_metadata,
            configured_mode=configured_mode,
            effective_mode=effective_mode,
            rust_mode=rust_mode,
        )
    sent_paths = dataplane.transmit_service_payload(SERVICE_ID_CONTROL_METADATA, payload)
    for path_name in path_ids:
        reserved.note_control_metadata_sent(
            control_metadata,
            payload,
            frame_bytes=len(payload) + GATHERLINK_V1_HEADER_LEN,
            path_metadata=path_metadata,
            path_capacity=path_capacity,
            path_latency=path_latency,
            path_mtu=path_mtu,
            path_pressure=path_pressure,
            service_metadata=service_metadata,
            service_endpoint_assertions=service_endpoint_assertions,
            service_scheduler_policies=service_scheduler_policies,
            service_disables=service_disables,
            sink_time=sink_time,
            internal_clock=control_metadata_helpers.clock_sync_sent_status(path_clock_sync),
            path_name=path_name,
        )
    return ControlMetadataAnnouncement(
        sent_paths=sent_paths,
        path_count=len(path_metadata),
        service_count=len(service_metadata),
        endpoint_assertion_count=len(service_endpoint_assertions),
        scheduler_policy_count=len(service_scheduler_policies),
        service_disable_count=len(service_disables),
        capacity_count=len(path_capacity),
        latency_count=len(path_latency),
        mtu_count=len(path_mtu),
        pressure_count=len(path_pressure),
        data_transmit_sample_count=len(data_transmit_samples),
        sink_time_count=len(sink_time),
        clock_sync_count=len(path_clock_sync),
        omitted_data_transmit_sample_count=max(0, original_data_transmit_sample_count - len(data_transmit_samples)),
        payload_bytes=len(payload),
    )


def _encode_control_payload_with_path_budget(
    runtime_config: Any,
    path_metadata: dict[int, str],
    data_transmit_samples: list[DataTransmitSample],
    payload_kwargs: dict[str, object],
) -> tuple[bytes, list[DataTransmitSample]]:
    """
    Encode control metadata while keeping sparse data-timing samples below path MTU.

    Python owns the policy choice to advertise real-data timing facts. Rust then
    executes the normal reserved-service send path, which cannot assume a large
    control frame will fit every active carrier. Non-sample metadata is kept
    intact; only opportunistic data timing samples are reduced when a path's
    compiled MTU budget is tight.
    """
    payload_budget = _smallest_control_payload_budget(runtime_config)
    if payload_budget is None or not data_transmit_samples:
        return (
            encode_control_payload(path_metadata, data_transmit_samples=data_transmit_samples, **payload_kwargs),
            data_transmit_samples,
        )

    base_payload = encode_control_payload(path_metadata, data_transmit_samples=[], **payload_kwargs)
    if len(base_payload) >= payload_budget:
        return base_payload, []

    # A DATA_TRANSMIT_SAMPLE is currently 1 byte type + 2 byte length + 22 bytes
    # of value. The final encode check keeps this robust if the compact wire
    # shape changes later.
    sample_wire_bytes = 25
    available_samples = min(len(data_transmit_samples), (payload_budget - len(base_payload)) // sample_wire_bytes)
    while available_samples > 0:
        selected_samples = data_transmit_samples[:available_samples]
        payload = encode_control_payload(path_metadata, data_transmit_samples=selected_samples, **payload_kwargs)
        if len(payload) <= payload_budget:
            return payload, selected_samples
        available_samples -= 1
    return base_payload, []


def _smallest_control_payload_budget(runtime_config: Any) -> int | None:
    """Return payload bytes that fit the smallest active path's Rust frame MTU."""
    budgets: list[int] = []
    for path in getattr(runtime_config, "paths", []):
        scheduler = getattr(path, "scheduler", None)
        if scheduler is None or not getattr(scheduler, "enabled", True):
            continue
        try:
            mtu = int(getattr(scheduler, "mtu"))
        except (TypeError, ValueError):
            continue
        budget = mtu - GATHERLINK_V1_HEADER_LEN
        if budget > 0:
            budgets.append(budget)
    return min(budgets) if budgets else None


def _service_path_policy_code(policy: str) -> int:
    """Encode Python's service path selector primitive into control metadata."""
    return {
        "inherit": 0,
        "single_best_path": 1,
        "weighted_round_robin": 2,
    }[policy]


def announce_path_pinned_clock_sync(
    dataplane: Any,
    runtime_config: Any,
    control_metadata: dict[str, object],
    path_clock_sync: list[InternalClockSyncMessage],
) -> PathPinnedControlAnnouncement:
    """
    Send NTP-style internal clock probes on the exact path they measure.

    This keeps Python in charge of clock-sync meaning while giving the Rust
    dataplane only the narrow execution primitive needed for honest per-path
    latency samples.
    """
    if not path_clock_sync:
        return PathPinnedControlAnnouncement(sent_paths=0, clock_sync_count=0, payload_bytes=0)
    path_names_by_id = {path.scheduler.path_id: path.name for path in runtime_config.paths}
    transmit_on_path = getattr(dataplane, "transmit_service_payload_on_path", None)
    if not callable(transmit_on_path):
        raise RuntimeError("Rust dataplane does not expose exact-path reserved-service transmit")
    sent_paths = 0
    payload_bytes = 0
    for sync in path_clock_sync:
        path_name = path_names_by_id.get(sync.path_id)
        if path_name is None:
            continue
        payload = encode_control_payload(
            {sync.path_id: path_name},
            path_clock_sync=[sync],
        )
        sent_paths += transmit_on_path(SERVICE_ID_CONTROL_METADATA, sync.path_id, payload)
        payload_bytes += len(payload)
        reserved.note_control_metadata_sent(
            control_metadata,
            payload,
            frame_bytes=len(payload) + GATHERLINK_V1_HEADER_LEN,
            path_metadata={sync.path_id: path_name},
            path_capacity={},
            path_latency={},
            path_mtu={},
            path_pressure={},
            internal_clock=control_metadata_helpers.clock_sync_sent_status([sync]),
            path_name=path_name,
        )
    return PathPinnedControlAnnouncement(
        sent_paths=sent_paths,
        clock_sync_count=len(path_clock_sync),
        payload_bytes=payload_bytes,
    )
