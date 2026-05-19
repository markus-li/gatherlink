"""Shared control metaband metadata and service-status helpers."""

from __future__ import annotations

import time
from contextlib import suppress
from datetime import UTC, datetime

from gatherlink.protocol import ControlFrame
from gatherlink.time.offset import InternalClockSyncMessage, SinkTimeMessage, internal_monotonic_us
from gatherlink.time.sink import read_system_ntp_status
from gatherlink.time.sources.direct_ntp import DirectNtpSample

NTP_STATE_UNKNOWN = 0
NTP_STATE_SYNCHRONIZED = 1
NTP_STATE_UNSYNCHRONIZED = 2


def empty_control_metadata() -> dict[str, object]:
    """Return the shared service-status shape for control metaband telemetry."""
    return {
        "sent": {"frames": 0, "messages": 0, "bytes": 0, "last_at": None},
        "received": {"frames": 0, "messages": 0, "bytes": 0, "last_at": None, "last_source": None},
        "path_control": {},
        "path_control_count": 0,
        "path_metadata": {},
        "path_metadata_count": 0,
        "service_metadata": {},
        "service_metadata_count": 0,
        "service_endpoint_assertions": {},
        "service_endpoint_assertion_count": 0,
        "service_disables": {},
        "service_disable_count": 0,
        "service_scheduler_policies": {},
        "service_scheduler_policy_count": 0,
        "service_endpoint_mismatches": {},
        "service_endpoint_mismatch_count": 0,
        "path_capacity": {},
        "path_capacity_count": 0,
        "path_latency": {},
        "path_latency_count": 0,
        "path_mtu": {},
        "path_mtu_count": 0,
        "internal_clock": {
            "role": None,
            "offset_us": None,
            "mean_offset_us": None,
            "rtt_us": None,
            "mean_rtt_us": None,
            "samples": 0,
            "path": None,
            "last_at": None,
        },
        "sink_time": {
            "role": None,
            "system_unix_us": None,
            "gatherlink_unix_us": None,
            "sink_sent_unix_us": None,
            "sink_sent_internal_us": None,
            "received_at": None,
            "received_internal_us": None,
            "path": None,
            "ntp_state": "unknown",
            "ntp_source": None,
            "ntp_source_type": None,
            "ntp_offset_us": None,
            "ntp_rtt_us": None,
            "ntp_stratum": None,
        },
    }


def sink_time_messages(
    path_names: list[str],
    path_ids: dict[str, int],
    ntp_sample: DirectNtpSample | None,
) -> dict[str, SinkTimeMessage]:
    """Build sink-authoritative wall-clock messages for each active return path."""
    sink_unix_us = ntp_sample.current_unix_us() if ntp_sample is not None else system_unix_us()
    sink_internal_us = internal_monotonic_us()
    # Direct NTP is a synchronized sink source. HTTPS Date fallback is useful
    # when UDP/123 is blocked, but it remains lower-confidence and must be
    # visible as non-NTP in monitor output and later policy decisions.
    if ntp_sample is not None and ntp_sample.source == "ntp":
        ntp_state = encode_ntp_state("synchronized")
    elif ntp_sample is not None:
        ntp_state = encode_ntp_state("unknown")
    else:
        ntp_state = encode_ntp_state(read_system_ntp_status())
    return {
        path_name: SinkTimeMessage(
            path_id=path_ids[path_name],
            sink_unix_us=sink_unix_us,
            sink_internal_us=sink_internal_us,
            ntp_state=ntp_state,
        )
        for path_name in path_names
    }


def record_sink_time(
    control_metadata: dict[str, object],
    sink_time_messages_: list[SinkTimeMessage],
    path_names_by_id: dict[int, str],
    *,
    received_at_internal_us: int,
    local_sink: bool = False,
    ntp_sample: DirectNtpSample | None = None,
) -> None:
    """Record the latest sink-authoritative wall-clock fact from control metadata."""
    if not sink_time_messages_:
        return
    message = sink_time_messages_[-1]
    sink_time = control_metadata["sink_time"]
    assert isinstance(sink_time, dict)
    sink_time.update(
        {
            "role": "sink-authoritative" if local_sink else "syncing-to-sink",
            "system_unix_us": system_unix_us(),
            "gatherlink_unix_us": message.sink_unix_us
            + (0 if local_sink else estimated_sink_one_way_us(control_metadata)),
            "sink_sent_unix_us": message.sink_unix_us,
            "sink_sent_internal_us": message.sink_internal_us,
            "received_at": datetime.now(UTC).isoformat(),
            "received_internal_us": received_at_internal_us,
            "path": path_names_by_id.get(message.path_id),
            "ntp_state": decode_ntp_state(message.ntp_state),
            "ntp_source": ntp_sample.server if ntp_sample is not None else None,
            "ntp_source_type": ntp_sample.source if ntp_sample is not None else None,
            "ntp_offset_us": ntp_sample.offset_us if ntp_sample is not None else None,
            "ntp_rtt_us": ntp_sample.rtt_us if ntp_sample is not None else None,
            "ntp_stratum": ntp_sample.stratum if ntp_sample is not None else None,
        }
    )


def refresh_gatherlink_time(control_metadata: dict[str, object]) -> None:
    """Advance display-time Gatherlink wall time from latest sink sample."""
    sink_time = control_metadata.get("sink_time")
    if not isinstance(sink_time, dict):
        return
    sent_unix_us = sink_time.get("sink_sent_unix_us")
    received_internal_us = sink_time.get("received_internal_us")
    if sent_unix_us is None or received_internal_us is None:
        return
    elapsed_us = max(internal_monotonic_us() - int(received_internal_us), 0)
    one_way_us = 0 if sink_time.get("role") == "sink-authoritative" else estimated_sink_one_way_us(control_metadata)
    sink_time["system_unix_us"] = system_unix_us()
    sink_time["gatherlink_unix_us"] = int(sent_unix_us) + elapsed_us + one_way_us


def estimated_sink_one_way_us(control_metadata: dict[str, object]) -> int:
    """
    Estimate sink-to-local one-way delay from the best current internal RTT.

    Until both directional latency confidence is available, the NTP-style
    internal clock exchange gives the least-wrong correction: use rolling mean
    RTT when present, fall back to current RTT, and divide by two. Python owns
    this policy so it can later swap in confidence-weighted one-way latency
    without changing the Rust fast path.
    """
    internal_clock = control_metadata.get("internal_clock")
    if not isinstance(internal_clock, dict):
        return 0
    rtt_us = internal_clock.get("mean_rtt_us") or internal_clock.get("rtt_us")
    if rtt_us is None:
        return 0
    with suppress(TypeError, ValueError):
        return max(int(rtt_us) // 2, 0)
    return 0


def system_unix_us() -> int:
    """Return current system wall time as Unix microseconds without changing it."""
    return time.time_ns() // 1000


def encode_ntp_state(value: str) -> int:
    """Encode an operator-readable NTP state into the compact control value."""
    if value == "synchronized":
        return NTP_STATE_SYNCHRONIZED
    if value == "unsynchronized":
        return NTP_STATE_UNSYNCHRONIZED
    return NTP_STATE_UNKNOWN


def decode_ntp_state(value: int) -> str:
    """Decode the compact control NTP state into operator-readable text."""
    if value == NTP_STATE_SYNCHRONIZED:
        return "synchronized"
    if value == NTP_STATE_UNSYNCHRONIZED:
        return "unsynchronized"
    return "unknown"


def record_control_metadata_sent(
    control_metadata: dict[str, object],
    frame_bytes: int,
    *,
    message_count: int,
    path_metadata: dict[int, str],
    path_capacity: dict[str, dict[str, int | str | None]],
    path_latency: dict[str, dict[str, int | str | None]],
    path_mtu: dict[str, dict[str, int | str | None]] | None = None,
    internal_clock: dict[str, int | str | None],
    sink_time: list[SinkTimeMessage],
    ntp_sample: DirectNtpSample | None = None,
    path_name: str | None = None,
) -> None:
    """Record one locally-sent control frame into the shared metadata shape."""
    sent = control_metadata["sent"]
    assert isinstance(sent, dict)
    sent["frames"] = int(sent["frames"]) + 1
    sent["messages"] = int(sent["messages"]) + message_count
    sent["bytes"] = int(sent["bytes"]) + frame_bytes
    sent["last_at"] = datetime.now(UTC).isoformat()
    if path_name is not None:
        record_path_control(control_metadata, path_name, "tx", frame_bytes, message_count)
    merge_control_path_metadata(control_metadata, path_metadata)
    merge_control_path_capacity(control_metadata, path_capacity)
    merge_control_path_latency(control_metadata, path_latency)
    merge_control_path_mtu(control_metadata, path_mtu or {})
    merge_internal_clock_sync(control_metadata, internal_clock)
    record_sink_time(
        control_metadata,
        sink_time,
        {},
        received_at_internal_us=internal_monotonic_us(),
        local_sink=True,
        ntp_sample=ntp_sample,
    )


def record_control_metadata_received(
    control_metadata: dict[str, object],
    frame_bytes: int,
    control_frame: ControlFrame,
    source: object,
    path_name: str | None = None,
) -> None:
    """Record one received control frame into the shared metadata shape."""
    received = control_metadata["received"]
    assert isinstance(received, dict)
    message_count = (
        len(control_frame.path_metadata)
        + len(control_frame.service_metadata)
        + len(control_frame.service_endpoint_assertions)
        + len(control_frame.service_disables)
        + len(control_frame.service_scheduler_policies)
        + len(control_frame.path_capacity_bps)
        + len(control_frame.path_latency_us)
        + len(control_frame.path_mtu)
        + len(control_frame.internal_clock_sync)
        + len(control_frame.sink_time)
    )
    received["frames"] = int(received["frames"]) + 1
    received["messages"] = int(received["messages"]) + message_count
    received["bytes"] = int(received["bytes"]) + frame_bytes
    received["last_at"] = datetime.now(UTC).isoformat()
    received["last_source"] = repr(source)
    if path_name is not None:
        record_path_control(control_metadata, path_name, "rx", frame_bytes, message_count)
    merge_control_path_metadata(control_metadata, control_frame.path_metadata)
    merge_control_service_metadata(control_metadata, control_frame.service_metadata)
    merge_control_service_endpoint_assertions(control_metadata, control_frame.service_endpoint_assertions)
    merge_control_service_disables(control_metadata, control_frame.service_disables)
    merge_control_service_scheduler_policies(control_metadata, control_frame.service_scheduler_policies)
    record_control_path_mtu(control_metadata, control_frame.path_mtu, control_frame.path_metadata, {})


def record_path_control(
    control_metadata: dict[str, object],
    path_name: str,
    direction: str,
    frame_bytes: int,
    message_count: int,
) -> None:
    """Count duplicated control traffic per transport path for monitor and scheduler visibility."""
    path_control = control_metadata["path_control"]
    assert isinstance(path_control, dict)
    path_entry = path_control.setdefault(
        path_name,
        {
            "tx": {"frames": 0, "messages": 0, "bytes": 0},
            "rx": {"frames": 0, "messages": 0, "bytes": 0},
        },
    )
    assert isinstance(path_entry, dict)
    counters = path_entry[direction]
    assert isinstance(counters, dict)
    counters["frames"] = int(counters["frames"]) + 1
    counters["messages"] = int(counters["messages"]) + message_count
    counters["bytes"] = int(counters["bytes"]) + frame_bytes
    control_metadata["path_control_count"] = len(path_control)


def merge_control_path_metadata(control_metadata: dict[str, object], path_metadata: dict[int, str]) -> None:
    """Merge path-id to path-name metadata without dropping older names."""
    recorded = control_metadata["path_metadata"]
    assert isinstance(recorded, dict)
    for path_id, path_name in path_metadata.items():
        recorded[str(path_id)] = path_name
    control_metadata["path_metadata_count"] = len(recorded)


def merge_control_service_metadata(control_metadata: dict[str, object], service_metadata: dict[int, str]) -> None:
    """Merge service-id to service-name metadata without accepting endpoints."""
    recorded = control_metadata["service_metadata"]
    assert isinstance(recorded, dict)
    for service_id, service_name in service_metadata.items():
        recorded[str(service_id)] = service_name
    control_metadata["service_metadata_count"] = len(recorded)


def merge_control_service_endpoint_assertions(
    control_metadata: dict[str, object],
    service_endpoint_assertions: dict[int, str],
) -> None:
    """Merge peer endpoint assertions as verification facts, not config."""
    recorded = control_metadata["service_endpoint_assertions"]
    assert isinstance(recorded, dict)
    for service_id, target in service_endpoint_assertions.items():
        recorded[str(service_id)] = target
    control_metadata["service_endpoint_assertion_count"] = len(recorded)


def merge_control_service_disables(
    control_metadata: dict[str, object],
    service_disables: dict[int, str],
) -> None:
    """Merge peer service-disable assertions for loud monitor and policy visibility."""
    recorded = control_metadata["service_disables"]
    assert isinstance(recorded, dict)
    for service_id, reason in service_disables.items():
        recorded[str(service_id)] = reason
    control_metadata["service_disable_count"] = len(recorded)


def merge_control_service_scheduler_policies(
    control_metadata: dict[str, object],
    service_scheduler_policies: dict[int, tuple[int, int]],
) -> None:
    """Merge peer-advertised service scheduler policy facts for Python to compile locally."""
    recorded = control_metadata["service_scheduler_policies"]
    assert isinstance(recorded, dict)
    for service_id, (fanout, fanout_below_bytes) in service_scheduler_policies.items():
        recorded[str(service_id)] = {
            "fanout": int(fanout),
            "fanout_below_bytes": int(fanout_below_bytes),
        }
    control_metadata["service_scheduler_policy_count"] = len(recorded)


def verify_service_endpoint_assertions(
    control_metadata: dict[str, object],
    local_targets_by_service_id: dict[int, str],
) -> dict[int, str]:
    """
    Compare peer endpoint assertions against explicit local config.

    The assertion data is never used to set config. A mismatch is recorded so
    the runtime can stop traffic and the service monitor can show the reason.
    """
    assertions = control_metadata.get("service_endpoint_assertions")
    if not isinstance(assertions, dict):
        return {}
    mismatches: dict[int, str] = {}
    for service_id_text, peer_target in assertions.items():
        try:
            service_id = int(service_id_text)
        except (TypeError, ValueError):
            continue
        local_target = local_targets_by_service_id.get(service_id)
        if local_target is None:
            mismatches[service_id] = f"peer asserted endpoint for unknown service id {service_id}: {peer_target}"
        elif str(peer_target) != local_target:
            mismatches[service_id] = (
                f"peer endpoint assertion mismatch for service id {service_id}: "
                f"local target {local_target} != peer target {peer_target}"
            )
    recorded = control_metadata["service_endpoint_mismatches"]
    assert isinstance(recorded, dict)
    for service_id, reason in mismatches.items():
        recorded[str(service_id)] = reason
    control_metadata["service_endpoint_mismatch_count"] = len(recorded)
    return mismatches


def merge_control_path_capacity(
    control_metadata: dict[str, object],
    path_capacity: dict[str, dict[str, int | str | None]],
) -> None:
    """Merge sparse path capacity updates without deleting the opposite direction."""
    recorded = control_metadata["path_capacity"]
    assert isinstance(recorded, dict)
    for path_name, capacity in path_capacity.items():
        existing = recorded.get(path_name)
        recorded[path_name] = merge_capacity_record(existing, capacity) if isinstance(existing, dict) else capacity
    control_metadata["path_capacity_count"] = len(recorded)


def merge_control_path_latency(
    control_metadata: dict[str, object],
    path_latency: dict[str, dict[str, int | str | None]],
) -> None:
    """Merge sparse path latency updates without deleting the opposite direction."""
    recorded = control_metadata["path_latency"]
    assert isinstance(recorded, dict)
    for path_name, latency in path_latency.items():
        existing = recorded.get(path_name)
        recorded[path_name] = merge_latency_record(existing, latency) if isinstance(existing, dict) else latency
    control_metadata["path_latency_count"] = len(recorded)


def merge_control_path_mtu(
    control_metadata: dict[str, object],
    path_mtu: dict[str, dict[str, int | str | None]],
) -> None:
    """Merge sparse directional path MTU observations without dropping the opposite direction."""
    recorded = control_metadata["path_mtu"]
    assert isinstance(recorded, dict)
    for path_name, mtu in path_mtu.items():
        existing = recorded.get(path_name)
        recorded[path_name] = merge_mtu_record(existing, mtu) if isinstance(existing, dict) else mtu
    control_metadata["path_mtu_count"] = len(recorded)


def merge_internal_clock_sync(
    control_metadata: dict[str, object],
    update: dict[str, int | str | None],
) -> None:
    """Merge the latest internal clock sync observation into service metadata."""
    if not update:
        return
    recorded = control_metadata["internal_clock"]
    assert isinstance(recorded, dict)
    for key, value in update.items():
        if value is None and recorded.get(key) is not None:
            continue
        recorded[key] = value


def record_control_path_capacity(
    control_metadata: dict[str, object],
    path_capacity_bps: dict[int, tuple[int | None, int | None]],
    learned_path_names_by_id: dict[int, str],
    configured_path_names_by_id: dict[int, str],
) -> None:
    """Store peer-advertised capacity in local directions using the best path name currently known."""
    named_capacity: dict[str, dict[str, int | str | None]] = {}
    for path_id, (tx_bps, rx_bps) in path_capacity_bps.items():
        path_name = (
            learned_path_names_by_id.get(path_id) or configured_path_names_by_id.get(path_id) or f"path-id:{path_id}"
        )
        named_capacity[path_name] = {
            "tx_bps": rx_bps,
            "rx_bps": tx_bps,
            "source": "peer",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    merge_control_path_capacity(control_metadata, named_capacity)


def record_control_path_latency(
    control_metadata: dict[str, object],
    path_latency_us: dict[int, tuple[int | None, int | None, int | None, int | None]],
    learned_path_names_by_id: dict[int, str],
    configured_path_names_by_id: dict[int, str],
) -> None:
    """Store peer-advertised latency in local directions using the best known path name."""
    named_latency: dict[str, dict[str, int | str | None]] = {}
    for path_id, (tx_current_us, tx_mean_us, rx_current_us, rx_mean_us) in path_latency_us.items():
        path_name = (
            learned_path_names_by_id.get(path_id) or configured_path_names_by_id.get(path_id) or f"path-id:{path_id}"
        )
        named_latency[path_name] = {
            "tx_current_us": rx_current_us,
            "tx_mean_us": rx_mean_us,
            "rx_current_us": tx_current_us,
            "rx_mean_us": tx_mean_us,
            "source": "peer",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    merge_control_path_latency(control_metadata, named_latency)


def record_control_path_mtu(
    control_metadata: dict[str, object],
    path_mtu: dict[int, tuple[int | None, int | None, int | None, int | None]],
    learned_path_names_by_id: dict[int, str],
    configured_path_names_by_id: dict[int, str],
) -> None:
    """Store peer-advertised MTU observations in local directions using the best known path name."""
    named_mtu: dict[str, dict[str, int | str | None]] = {}
    for path_id, (tx_link_mtu, tx_frame_mtu, rx_link_mtu, rx_frame_mtu) in path_mtu.items():
        path_name = (
            learned_path_names_by_id.get(path_id) or configured_path_names_by_id.get(path_id) or f"path-id:{path_id}"
        )
        header_len = _known_payload_header_len(control_metadata, path_name)
        named_mtu[path_name] = {
            "tx_link_mtu": rx_link_mtu,
            "tx_frame_mtu": rx_frame_mtu,
            "tx_payload_mtu": payload_mtu_or_none(rx_frame_mtu, header_len=header_len),
            "rx_link_mtu": tx_link_mtu,
            "rx_frame_mtu": tx_frame_mtu,
            "rx_payload_mtu": payload_mtu_or_none(tx_frame_mtu, header_len=header_len),
            "source": "peer",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    merge_control_path_mtu(control_metadata, named_mtu)


def clock_sync_sent_status(path_clock_sync: list[InternalClockSyncMessage]) -> dict[str, int | str | None]:
    """Return service-status fields for the latest outbound clock-sync request."""
    if not path_clock_sync:
        return {}
    sync = path_clock_sync[-1]
    return {
        "role": "syncing-to-sink",
        "last_sent_exchange_id": sync.exchange_id,
        "last_sent_path_id": sync.path_id,
        "last_at": datetime.now(UTC).isoformat(),
    }


def sink_clock_sync_responses(
    clock_sync_messages: list[InternalClockSyncMessage],
    *,
    received_at_us: int,
) -> list[InternalClockSyncMessage]:
    """Build sink-authoritative NTP-style internal clock sync responses."""
    responses = []
    for message in clock_sync_messages:
        if message.mode != 1:
            continue
        transmit_us = internal_monotonic_us()
        responses.append(
            InternalClockSyncMessage(
                exchange_id=message.exchange_id,
                path_id=message.path_id,
                mode=2,
                origin_us=message.origin_us,
                receive_us=received_at_us,
                transmit_us=transmit_us,
            )
        )
    return responses


def capacity_by_path_id(
    path_capacity: dict[str, dict[str, int | str | None]],
    path_ids: dict[str, int],
) -> dict[int, tuple[int | None, int | None]]:
    """Convert named local-view capacity metadata into path-id keyed control payloads."""
    capacity_by_id = {}
    for path_name, capacity in path_capacity.items():
        if path_name not in path_ids:
            continue
        capacity_by_id[path_ids[path_name]] = (
            int_or_none(capacity.get("tx_bps")),
            int_or_none(capacity.get("rx_bps")),
        )
    return capacity_by_id


def latency_by_path_id(
    path_latency: dict[str, dict[str, int | str | None]],
    path_ids: dict[str, int],
) -> dict[int, tuple[int | None, int | None, int | None, int | None]]:
    """Convert named local-view latency metadata into path-id keyed control payloads."""
    latency_by_id = {}
    for path_name, latency in path_latency.items():
        if path_name not in path_ids:
            continue
        latency_by_id[path_ids[path_name]] = (
            int_or_none(latency.get("tx_current_us")),
            int_or_none(latency.get("tx_mean_us")),
            int_or_none(latency.get("rx_current_us")),
            int_or_none(latency.get("rx_mean_us")),
        )
    return latency_by_id


def mtu_by_path_id(
    path_mtu: dict[str, dict[str, int | str | None]],
    path_ids: dict[str, int],
) -> dict[int, tuple[int | None, int | None, int | None, int | None]]:
    """Convert named local-view MTU observations into path-id keyed control payloads."""
    mtu_by_id = {}
    for path_name, mtu in path_mtu.items():
        if path_name not in path_ids:
            continue
        tx_link_mtu = int_or_none(mtu.get("tx_link_mtu", mtu.get("link_mtu")))
        tx_frame_mtu = int_or_none(mtu.get("tx_frame_mtu", mtu.get("frame_mtu")))
        rx_link_mtu = int_or_none(mtu.get("rx_link_mtu"))
        rx_frame_mtu = int_or_none(mtu.get("rx_frame_mtu"))
        if tx_link_mtu is None and rx_link_mtu is None:
            continue
        mtu_by_id[path_ids[path_name]] = (tx_link_mtu, tx_frame_mtu, rx_link_mtu, rx_frame_mtu)
    return mtu_by_id


def int_or_none(value: object) -> int | None:
    """Return an integer when a value is numeric, otherwise ``None``."""
    if value is None:
        return None
    with suppress(TypeError, ValueError):
        return int(value)
    return None


def merge_capacity_record(
    existing: dict[str, int | str | None],
    update: dict[str, int | str | None],
) -> dict[str, int | str | None]:
    """Merge sparse capacity records while preserving known tx/rx values."""
    merged = dict(existing)
    for key, value in update.items():
        if value is None and key in {"tx_bps", "rx_bps"} and merged.get(key) is not None:
            continue
        merged[key] = value
    return merged


def merge_latency_record(
    existing: dict[str, int | str | None],
    update: dict[str, int | str | None],
) -> dict[str, int | str | None]:
    """Merge sparse latency updates without deleting the opposite direction."""
    merged = dict(existing)
    for key, value in update.items():
        if value is None and key in {"tx_current_us", "tx_mean_us", "rx_current_us", "rx_mean_us"}:
            continue
        merged[key] = value
    return merged


def merge_mtu_record(
    existing: dict[str, int | str | None],
    update: dict[str, int | str | None],
) -> dict[str, int | str | None]:
    """Merge sparse MTU updates while preserving known directional values."""
    merged = dict(existing)
    directional_keys = {
        "tx_link_mtu",
        "tx_frame_mtu",
        "tx_payload_mtu",
        "rx_link_mtu",
        "rx_frame_mtu",
        "rx_payload_mtu",
    }
    for key, value in update.items():
        if value is None and key in directional_keys and merged.get(key) is not None:
            continue
        merged[key] = value

    # Keep legacy display keys aligned with TX because older running services
    # and ad hoc diagnostics may still read the old flat names during rollout.
    if merged.get("tx_link_mtu") is not None:
        merged["link_mtu"] = merged["tx_link_mtu"]
    if merged.get("tx_frame_mtu") is not None:
        merged["frame_mtu"] = merged["tx_frame_mtu"]
    if merged.get("tx_payload_mtu") is not None:
        merged["payload_mtu"] = merged["tx_payload_mtu"]
    return merged


def payload_mtu_or_none(frame_mtu: int | None, *, header_len: int = 14) -> int | None:
    """Return compact v1 normal payload MTU for a reported frame MTU."""
    return max(frame_mtu - header_len, 0) if frame_mtu is not None else None


def _known_payload_header_len(control_metadata: dict[str, object], path_name: str) -> int:
    """Infer whether the running service is advertising compact v1 or v2 payload space."""
    path_mtu = control_metadata.get("path_mtu")
    if not isinstance(path_mtu, dict):
        return 14
    current = path_mtu.get(path_name)
    if not isinstance(current, dict):
        return 14
    frame_mtu = current.get("tx_frame_mtu")
    payload_mtu = current.get("tx_payload_mtu")
    if isinstance(frame_mtu, int) and isinstance(payload_mtu, int):
        header_len = frame_mtu - payload_mtu
        if header_len in {13, 14}:
            return header_len
    return 14
