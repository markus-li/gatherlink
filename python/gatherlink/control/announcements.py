"""Outbound control metadata announcements owned by the Python control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gatherlink.control import metadata as control_metadata_helpers
from gatherlink.control import reserved
from gatherlink.protocol import GATHERLINK_V1_HEADER_LEN, SERVICE_ID_CONTROL_METADATA, encode_control_payload


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
    mtu_count: int
    sink_time_count: int
    payload_bytes: int


def announce_control_metadata(
    dataplane: Any,
    runtime_config: Any,
    control_metadata: dict[str, object],
    *,
    path_capacity: dict[str, dict[str, int | str | None]] | None = None,
    path_mtu: dict[str, dict[str, int | str | None]] | None = None,
    service_disables: dict[int, str] | None = None,
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
        service.service_id: (service.scheduler_fanout, service.scheduler_fanout_below_bytes)
        for service in runtime_config.services
    }
    sink_time = (
        list(control_metadata_helpers.sink_time_messages(list(path_ids), path_ids, ntp_sample).values())
        if include_sink_time
        else []
    )
    path_capacity = path_capacity or {}
    path_mtu = path_mtu or {}
    service_disables = service_disables or {}
    payload = encode_control_payload(
        path_metadata,
        service_metadata=service_metadata,
        service_endpoint_assertions=service_endpoint_assertions,
        service_scheduler_policies=service_scheduler_policies,
        service_disables=service_disables,
        path_capacity_bps=control_metadata_helpers.capacity_by_path_id(path_capacity, path_ids),
        path_mtu=control_metadata_helpers.mtu_by_path_id(path_mtu, path_ids),
        sink_time=sink_time,
    )
    sent_paths = dataplane.transmit_service_payload(SERVICE_ID_CONTROL_METADATA, payload)
    for path_name in path_ids:
        reserved.note_control_metadata_sent(
            control_metadata,
            payload,
            frame_bytes=len(payload) + GATHERLINK_V1_HEADER_LEN,
            path_metadata=path_metadata,
            path_capacity=path_capacity,
            path_mtu=path_mtu,
            service_metadata=service_metadata,
            service_endpoint_assertions=service_endpoint_assertions,
            service_scheduler_policies=service_scheduler_policies,
            service_disables=service_disables,
            sink_time=sink_time,
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
        mtu_count=len(path_mtu),
        sink_time_count=len(sink_time),
        payload_bytes=len(payload),
    )
