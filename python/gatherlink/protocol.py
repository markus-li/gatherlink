"""
Gatherlink v1 frame and control metaband helpers.

This module mirrors the Rust protocol crate for Python control-plane code that
needs to build or inspect frames. It intentionally contains wire
encoding/decoding only; policy, lab shaping, process lifecycle, and scheduling
decisions stay elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from gatherlink.time.offset import InternalClockSyncMessage, SinkTimeMessage

GATHERLINK_PROTOCOL_VERSION = 1
GATHERLINK_KIND_DATA = 0
GATHERLINK_KIND_CONTROL = 1
GATHERLINK_KIND_BATCH = 2
GATHERLINK_KIND_MASK = 0b0000_0011
GATHERLINK_FRAGMENT_PRESENT = 0b0000_0100
GATHERLINK_KIND_FLAGS_RESERVED_MASK = 0b1111_1000
GATHERLINK_V1_HEADER_LEN = 14
GATHERLINK_FRAGMENT_METADATA_LEN = 10
SERVICE_ID_INVALID = 0
SERVICE_ID_CONTROL_METADATA = 1
SERVICE_ID_TIME_SYNC = 2
SERVICE_ID_INTERNAL_DNS = 3
SERVICE_ID_PATH_DISCOVERY = 4
SERVICE_ID_DIAGNOSTICS = 5
SERVICE_ID_CONFIG_APPLY = 6
SERVICE_ID_AUTH_CRYPTO = 7
SERVICE_ID_REMOTE_STATUS = 8
RESERVED_SERVICE_ID_END = 255
USER_SERVICE_ID_START = RESERVED_SERVICE_ID_END + 1
CONTROL_PAYLOAD_VERSION = 1
CONTROL_TYPE_PATH_METADATA = 3
CONTROL_TYPE_PATH_CAPACITY = 4
CONTROL_TYPE_PATH_LATENCY = 5
CONTROL_TYPE_INTERNAL_CLOCK_SYNC = 6
CONTROL_TYPE_SINK_TIME = 7
CONTROL_TYPE_SERVICE_METADATA = 8
CONTROL_TYPE_SERVICE_ENDPOINT_ASSERTION = 9
CONTROL_TYPE_SERVICE_DISABLE = 10
CONTROL_TYPE_PATH_MTU = 11
CONTROL_TYPE_SERVICE_SCHEDULER_POLICY = 12
CONTROL_TYPE_PATH_PRESSURE = 13
CONTROL_TYPE_SCHEDULER_STATUS = 14
CONTROL_TYPE_DATA_TRANSMIT_SAMPLE = 15
CONTROL_TYPE_PATH_LATENCY_QUALITY = 16
CONTROL_TYPE_PATH_LATENCY_STATS = 17
SEQUENCE_SPACE = 1 << 64
DEFAULT_SESSION_ID = 0
DEFAULT_SERVICE_ID = USER_SERVICE_ID_START

ServiceSchedulerPolicy = tuple[int, int, int, int, int, int]
PathPressure = tuple[int, int, int, int, int, int, int, int, int, int, int, int, int, int]
SchedulerStatus = tuple[str, str, str]
DataTransmitSample = tuple[int, int, int, int]
PathLatencyQuality = tuple[str | None, str | None]
PathLatencyStats = tuple[int | None, int | None, int | None, int | None, int | None, int | None]


@dataclass(frozen=True)
class DataFrame:
    """Decoded v1 data frame used by Python control-plane and lab code."""

    path_id: int
    sequence: int
    payload: bytes


@dataclass(frozen=True)
class ControlFrame:
    """Decoded control frame fields used for peer names and telemetry."""

    path_metadata: dict[int, str]
    service_metadata: dict[int, str]
    service_endpoint_assertions: dict[int, str]
    service_disables: dict[int, str]
    service_scheduler_policies: dict[int, ServiceSchedulerPolicy]
    path_capacity_bps: dict[int, tuple[int | None, int | None]]
    path_latency_us: dict[int, tuple[int | None, int | None, int | None, int | None]]
    path_latency_quality: dict[int, PathLatencyQuality]
    path_latency_stats: dict[int, PathLatencyStats]
    path_mtu: dict[int, tuple[int | None, int | None, int | None, int | None]]
    path_pressure: dict[int, PathPressure]
    scheduler_status: SchedulerStatus | None
    data_transmit_samples: list[DataTransmitSample]
    internal_clock_sync: list[InternalClockSyncMessage]
    sink_time: list[SinkTimeMessage]


def encode_data_frame(sequence: int, path_id: int, payload: bytes) -> bytes:
    """
    Encapsulate normal UDP traffic in the same fixed v1 frame shape Rust defines.

    The user packet is untouched inside the payload. Sequence and path id live
    in the Gatherlink path frame, which is the layer where real loss and
    reordering telemetry belongs.
    """
    if sequence < 0 or sequence >= SEQUENCE_SPACE or path_id < 0 or path_id > _u16_max():
        raise ValueError("protocol data frame metadata value out of range")
    if len(payload) > _u16_max():
        raise ValueError("protocol data frame payload is too large")

    header = bytearray(GATHERLINK_V1_HEADER_LEN)
    header[0] = GATHERLINK_PROTOCOL_VERSION
    header[1] = GATHERLINK_KIND_DATA
    _write_u16(header, 2, DEFAULT_SERVICE_ID)
    _write_u16(header, 4, path_id)
    _write_u64(header, 6, sequence)
    return bytes(header) + payload


def encode_control_frame(path_id: int, payload: bytes) -> bytes:
    """Encode a v1 control frame for sparse peer metadata."""
    if path_id < 0 or path_id > _u16_max() or len(payload) > _u16_max():
        raise ValueError("protocol control frame value out of range")
    header = bytearray(GATHERLINK_V1_HEADER_LEN)
    header[0] = GATHERLINK_PROTOCOL_VERSION
    header[1] = GATHERLINK_KIND_CONTROL
    _write_u16(header, 2, SERVICE_ID_CONTROL_METADATA)
    _write_u16(header, 4, path_id)
    _write_u64(header, 6, 0)
    return bytes(header) + payload


def decode_data_frame(payload: bytes) -> DataFrame | None:
    """Decode one v1 data frame and return the original UDP payload."""
    if len(payload) < GATHERLINK_V1_HEADER_LEN:
        return None
    if payload[0] != GATHERLINK_PROTOCOL_VERSION:
        return None
    kind_flags = payload[1]
    if kind_flags & GATHERLINK_KIND_FLAGS_RESERVED_MASK or (kind_flags & GATHERLINK_KIND_MASK) != GATHERLINK_KIND_DATA:
        return None
    header_len = GATHERLINK_V1_HEADER_LEN
    if kind_flags & GATHERLINK_FRAGMENT_PRESENT:
        header_len += GATHERLINK_FRAGMENT_METADATA_LEN
    if len(payload) < header_len:
        return None

    return DataFrame(
        path_id=_read_u16(payload, 4),
        sequence=_read_u64(payload, 6),
        payload=payload[header_len:],
    )


def decode_control_frame(payload: bytes) -> ControlFrame | None:
    """Decode the Python-supported subset of the v1 control metaband."""
    if len(payload) < GATHERLINK_V1_HEADER_LEN:
        return None
    if payload[0] != GATHERLINK_PROTOCOL_VERSION:
        return None
    kind_flags = payload[1]
    if (
        kind_flags & GATHERLINK_KIND_FLAGS_RESERVED_MASK
        or (kind_flags & GATHERLINK_KIND_MASK) != GATHERLINK_KIND_CONTROL
    ):
        return None
    header_len = GATHERLINK_V1_HEADER_LEN
    if kind_flags & GATHERLINK_FRAGMENT_PRESENT:
        header_len += GATHERLINK_FRAGMENT_METADATA_LEN
    if len(payload) < header_len:
        return None

    return decode_control_payload(payload[header_len:])


def encode_control_payload(
    path_metadata: dict[int, str],
    service_metadata: dict[int, str] | None = None,
    service_endpoint_assertions: dict[int, str] | None = None,
    service_disables: dict[int, str] | None = None,
    service_scheduler_policies: dict[int, ServiceSchedulerPolicy] | None = None,
    path_capacity_bps: dict[int, tuple[int | None, int | None]] | None = None,
    path_latency_us: dict[int, tuple[int | None, int | None, int | None, int | None]] | None = None,
    path_latency_quality: dict[int, PathLatencyQuality] | None = None,
    path_latency_stats: dict[int, PathLatencyStats] | None = None,
    path_mtu: dict[int, tuple[int | None, int | None, int | None, int | None]] | None = None,
    path_pressure: dict[int, PathPressure] | None = None,
    scheduler_status: SchedulerStatus | None = None,
    data_transmit_samples: list[DataTransmitSample] | None = None,
    path_clock_sync: list[InternalClockSyncMessage] | None = None,
    sink_time: list[SinkTimeMessage] | None = None,
) -> bytes:
    """Encode optional control messages; absent fields cost no bytes on the wire."""
    service_metadata = service_metadata or {}
    service_endpoint_assertions = service_endpoint_assertions or {}
    service_disables = service_disables or {}
    service_scheduler_policies = service_scheduler_policies or {}
    path_capacity_bps = path_capacity_bps or {}
    path_latency_us = path_latency_us or {}
    path_latency_quality = path_latency_quality or {}
    path_latency_stats = path_latency_stats or {}
    path_mtu = path_mtu or {}
    path_pressure = path_pressure or {}
    path_clock_sync = path_clock_sync or []
    data_transmit_samples = data_transmit_samples or []
    sink_time = sink_time or []
    message_count = (
        len(path_metadata)
        + len(service_metadata)
        + len(service_endpoint_assertions)
        + len(service_disables)
        + len(service_scheduler_policies)
        + len(path_capacity_bps)
        + len(path_latency_us)
        + len(path_latency_quality)
        + len(path_latency_stats)
        + len(path_mtu)
        + len(path_pressure)
        + (1 if scheduler_status is not None else 0)
        + len(data_transmit_samples)
        + len(path_clock_sync)
        + len(sink_time)
    )
    if message_count == 0:
        raise ValueError("control payload must include at least one message")
    output = bytearray()
    output.append(CONTROL_PAYLOAD_VERSION)
    _append_u16(output, message_count)
    _encode_path_metadata(output, path_metadata)
    _encode_service_metadata(output, service_metadata)
    _encode_service_endpoint_assertions(output, service_endpoint_assertions)
    _encode_service_disables(output, service_disables)
    _encode_service_scheduler_policies(output, service_scheduler_policies)
    _encode_path_capacity(output, path_capacity_bps)
    _encode_path_latency(output, path_latency_us)
    _encode_path_latency_quality(output, path_latency_quality)
    _encode_path_latency_stats(output, path_latency_stats)
    _encode_path_mtu(output, path_mtu)
    _encode_path_pressure(output, path_pressure)
    _encode_scheduler_status(output, scheduler_status)
    _encode_data_transmit_samples(output, data_transmit_samples)
    _encode_internal_clock_sync(output, path_clock_sync)
    _encode_sink_time(output, sink_time)
    if len(output) > _u16_max():
        raise ValueError("control payload is too large")
    return bytes(output)


def encode_control_payload_path_metadata(path_metadata: dict[int, str]) -> bytes:
    """Encode v1 PathMetadata messages so receivers can name paths by control data."""
    return encode_control_payload(path_metadata)


def decode_control_payload(payload: bytes) -> ControlFrame | None:
    """Decode known v1 control messages and ignore unknown optional messages."""
    if len(payload) < 3 or payload[0] != CONTROL_PAYLOAD_VERSION:
        return None
    message_count = _read_u16(payload, 1)
    cursor = 3
    path_metadata: dict[int, str] = {}
    service_metadata: dict[int, str] = {}
    service_endpoint_assertions: dict[int, str] = {}
    service_disables: dict[int, str] = {}
    service_scheduler_policies: dict[int, ServiceSchedulerPolicy] = {}
    path_capacity_bps: dict[int, tuple[int | None, int | None]] = {}
    path_latency_us: dict[int, tuple[int | None, int | None, int | None, int | None]] = {}
    path_latency_quality: dict[int, PathLatencyQuality] = {}
    path_latency_stats: dict[int, PathLatencyStats] = {}
    path_mtu: dict[int, tuple[int | None, int | None, int | None, int | None]] = {}
    path_pressure: dict[int, PathPressure] = {}
    scheduler_status: SchedulerStatus | None = None
    data_transmit_samples: list[DataTransmitSample] = []
    internal_clock_sync: list[InternalClockSyncMessage] = []
    sink_time: list[SinkTimeMessage] = []
    for _ in range(message_count):
        if cursor + 3 > len(payload):
            return None
        message_type = payload[cursor]
        value_len = _read_u16(payload, cursor + 1)
        cursor += 3
        if cursor + value_len > len(payload):
            return None
        value = payload[cursor : cursor + value_len]
        cursor += value_len
        if message_type == CONTROL_TYPE_PATH_METADATA:
            decoded = _decode_path_metadata_value(value)
            if decoded is None:
                return None
            path_id, path_name = decoded
            path_metadata[path_id] = path_name
        elif message_type == CONTROL_TYPE_SERVICE_METADATA:
            decoded = _decode_service_metadata_value(value)
            if decoded is None:
                return None
            service_id, service_name = decoded
            service_metadata[service_id] = service_name
        elif message_type == CONTROL_TYPE_SERVICE_ENDPOINT_ASSERTION:
            decoded = _decode_service_endpoint_assertion_value(value)
            if decoded is None:
                return None
            service_id, target = decoded
            service_endpoint_assertions[service_id] = target
        elif message_type == CONTROL_TYPE_SERVICE_DISABLE:
            decoded = _decode_service_disable_value(value)
            if decoded is None:
                return None
            service_id, reason = decoded
            service_disables[service_id] = reason
        elif message_type == CONTROL_TYPE_SERVICE_SCHEDULER_POLICY:
            decoded_policy = _decode_service_scheduler_policy_value(value)
            if decoded_policy is None:
                return None
            (
                service_id,
                fanout,
                fanout_below_bytes,
                flowlet_idle_us,
                flowlet_max_hold_us,
                path_run_datagrams,
                path_policy,
            ) = decoded_policy
            service_scheduler_policies[service_id] = (
                fanout,
                fanout_below_bytes,
                flowlet_idle_us,
                flowlet_max_hold_us,
                path_run_datagrams,
                path_policy,
            )
        elif message_type == CONTROL_TYPE_PATH_CAPACITY:
            decoded_capacity = _decode_path_capacity_value(value)
            if decoded_capacity is None:
                return None
            path_id, tx_bps, rx_bps = decoded_capacity
            path_capacity_bps[path_id] = (tx_bps, rx_bps)
        elif message_type == CONTROL_TYPE_PATH_LATENCY:
            decoded_latency = _decode_path_latency_value(value)
            if decoded_latency is None:
                return None
            path_id, tx_current_us, tx_mean_us, rx_current_us, rx_mean_us = decoded_latency
            path_latency_us[path_id] = (tx_current_us, tx_mean_us, rx_current_us, rx_mean_us)
        elif message_type == CONTROL_TYPE_PATH_LATENCY_QUALITY:
            decoded_latency_quality = _decode_path_latency_quality_value(value)
            if decoded_latency_quality is None:
                return None
            path_id, source, confidence = decoded_latency_quality
            path_latency_quality[path_id] = (source, confidence)
        elif message_type == CONTROL_TYPE_PATH_LATENCY_STATS:
            decoded_latency_stats = _decode_path_latency_stats_value(value)
            if decoded_latency_stats is None:
                return None
            path_id, rtt_us, clock_error_us, tx_jitter_us, rx_jitter_us, tx_p95_us, rx_p95_us = decoded_latency_stats
            path_latency_stats[path_id] = (rtt_us, clock_error_us, tx_jitter_us, rx_jitter_us, tx_p95_us, rx_p95_us)
        elif message_type == CONTROL_TYPE_PATH_MTU:
            decoded_mtu = _decode_path_mtu_value(value)
            if decoded_mtu is None:
                return None
            path_id, tx_link_mtu, tx_frame_mtu, rx_link_mtu, rx_frame_mtu = decoded_mtu
            path_mtu[path_id] = (tx_link_mtu, tx_frame_mtu, rx_link_mtu, rx_frame_mtu)
        elif message_type == CONTROL_TYPE_PATH_PRESSURE:
            decoded_pressure = _decode_path_pressure_value(value)
            if decoded_pressure is None:
                return None
            (
                path_id,
                loss_ppm,
                queue_depth_packets,
                queue_depth_bytes,
                queue_oldest_age_us,
                send_failures,
                receive_gaps,
                reorder_depth_packets,
                local_drops,
                scheduler_in_flight_packets,
                scheduler_in_flight_bytes,
                scheduler_predicted_delivery_us,
                reorder_buffer_packets,
                reorder_buffer_oldest_age_us,
                observed_packets,
            ) = decoded_pressure
            path_pressure[path_id] = (
                loss_ppm,
                queue_depth_packets,
                queue_depth_bytes,
                queue_oldest_age_us,
                send_failures,
                receive_gaps,
                reorder_depth_packets,
                local_drops,
                scheduler_in_flight_packets,
                scheduler_in_flight_bytes,
                scheduler_predicted_delivery_us,
                reorder_buffer_packets,
                reorder_buffer_oldest_age_us,
                observed_packets,
            )
        elif message_type == CONTROL_TYPE_SCHEDULER_STATUS:
            scheduler_status = _decode_scheduler_status_value(value)
            if scheduler_status is None:
                return None
        elif message_type == CONTROL_TYPE_DATA_TRANSMIT_SAMPLE:
            decoded_sample = _decode_data_transmit_sample_value(value)
            if decoded_sample is None:
                return None
            data_transmit_samples.append(decoded_sample)
        elif message_type == CONTROL_TYPE_INTERNAL_CLOCK_SYNC:
            decoded_sync = _decode_internal_clock_sync_value(value)
            if decoded_sync is None:
                return None
            internal_clock_sync.append(decoded_sync)
        elif message_type == CONTROL_TYPE_SINK_TIME:
            decoded_sink_time = _decode_sink_time_value(value)
            if decoded_sink_time is None:
                return None
            sink_time.append(decoded_sink_time)
    if cursor != len(payload):
        return None
    return ControlFrame(
        path_metadata=path_metadata,
        service_metadata=service_metadata,
        service_endpoint_assertions=service_endpoint_assertions,
        service_disables=service_disables,
        service_scheduler_policies=service_scheduler_policies,
        path_capacity_bps=path_capacity_bps,
        path_latency_us=path_latency_us,
        path_latency_quality=path_latency_quality,
        path_latency_stats=path_latency_stats,
        path_mtu=path_mtu,
        path_pressure=path_pressure,
        scheduler_status=scheduler_status,
        data_transmit_samples=data_transmit_samples,
        internal_clock_sync=internal_clock_sync,
        sink_time=sink_time,
    )


def _encode_path_metadata(output: bytearray, path_metadata: dict[int, str]) -> None:
    for path_id, path_name in path_metadata.items():
        encoded_name = path_name.encode("utf-8")
        if path_id < 0 or path_id > _u16_max() or not encoded_name or len(encoded_name) > 255:
            raise ValueError("path metadata value out of range")
        output.append(CONTROL_TYPE_PATH_METADATA)
        _append_u16(output, 3 + len(encoded_name))
        _append_u16(output, path_id)
        output.append(len(encoded_name))
        output.extend(encoded_name)


def _encode_service_metadata(output: bytearray, service_metadata: dict[int, str]) -> None:
    for service_id, service_name in service_metadata.items():
        encoded_name = service_name.encode("utf-8")
        if service_id < USER_SERVICE_ID_START or service_id > _u16_max() or not encoded_name or len(encoded_name) > 255:
            raise ValueError("service metadata value out of range")
        output.append(CONTROL_TYPE_SERVICE_METADATA)
        _append_u16(output, 3 + len(encoded_name))
        _append_u16(output, service_id)
        output.append(len(encoded_name))
        output.extend(encoded_name)


def _encode_service_endpoint_assertions(output: bytearray, service_endpoint_assertions: dict[int, str]) -> None:
    for service_id, target in service_endpoint_assertions.items():
        encoded_target = target.encode("utf-8")
        if (
            service_id < USER_SERVICE_ID_START
            or service_id > _u16_max()
            or not encoded_target
            or len(encoded_target) > 255
        ):
            raise ValueError("service endpoint assertion value out of range")
        output.append(CONTROL_TYPE_SERVICE_ENDPOINT_ASSERTION)
        _append_u16(output, 3 + len(encoded_target))
        _append_u16(output, service_id)
        output.append(len(encoded_target))
        output.extend(encoded_target)


def _encode_service_disables(output: bytearray, service_disables: dict[int, str]) -> None:
    for service_id, reason in service_disables.items():
        encoded_reason = reason.encode("utf-8")
        if (
            service_id < USER_SERVICE_ID_START
            or service_id > _u16_max()
            or not encoded_reason
            or len(encoded_reason) > 255
        ):
            raise ValueError("service disable value out of range")
        output.append(CONTROL_TYPE_SERVICE_DISABLE)
        _append_u16(output, 3 + len(encoded_reason))
        _append_u16(output, service_id)
        output.append(len(encoded_reason))
        output.extend(encoded_reason)


def _encode_service_scheduler_policies(
    output: bytearray, service_scheduler_policies: dict[int, ServiceSchedulerPolicy]
) -> None:
    for service_id, policy in service_scheduler_policies.items():
        if len(policy) == 5:
            fanout, fanout_below_bytes, flowlet_idle_us, flowlet_max_hold_us, path_run_datagrams = policy
            path_policy = 0
        else:
            (
                fanout,
                fanout_below_bytes,
                flowlet_idle_us,
                flowlet_max_hold_us,
                path_run_datagrams,
                path_policy,
            ) = policy
        if (
            service_id < USER_SERVICE_ID_START
            or service_id > _u16_max()
            or fanout < 0
            or fanout > _u16_max()
            or fanout_below_bytes < 0
            or fanout_below_bytes > _u32_max()
            or flowlet_idle_us < 0
            or flowlet_idle_us > _u64_max()
            or flowlet_max_hold_us < 0
            or flowlet_max_hold_us > _u64_max()
            or path_run_datagrams < 0
            or path_run_datagrams > _u32_max()
            or path_policy < 0
            or path_policy > 2
        ):
            raise ValueError("service scheduler policy value out of range")
        output.append(CONTROL_TYPE_SERVICE_SCHEDULER_POLICY)
        _append_u16(output, 29)
        _append_u16(output, service_id)
        _append_u16(output, fanout)
        _append_u32(output, fanout_below_bytes)
        _append_u64(output, flowlet_idle_us)
        _append_u64(output, flowlet_max_hold_us)
        _append_u32(output, path_run_datagrams)
        output.append(path_policy)


def _encode_path_capacity(output: bytearray, path_capacity_bps: dict[int, tuple[int | None, int | None]]) -> None:
    for path_id, (tx_bps, rx_bps) in path_capacity_bps.items():
        if path_id < 0 or path_id > _u16_max():
            raise ValueError("path capacity path id out of range")
        output.append(CONTROL_TYPE_PATH_CAPACITY)
        _append_u16(output, 18)
        _append_u16(output, path_id)
        _append_u64(output, _optional_u64(tx_bps))
        _append_u64(output, _optional_u64(rx_bps))


def _encode_path_latency(
    output: bytearray,
    path_latency_us: dict[int, tuple[int | None, int | None, int | None, int | None]],
) -> None:
    for path_id, (tx_current_us, tx_mean_us, rx_current_us, rx_mean_us) in path_latency_us.items():
        if path_id < 0 or path_id > _u16_max():
            raise ValueError("path latency path id out of range")
        output.append(CONTROL_TYPE_PATH_LATENCY)
        _append_u16(output, 18)
        _append_u16(output, path_id)
        _append_u32(output, _optional_u32(tx_current_us))
        _append_u32(output, _optional_u32(tx_mean_us))
        _append_u32(output, _optional_u32(rx_current_us))
        _append_u32(output, _optional_u32(rx_mean_us))


def _encode_path_latency_quality(output: bytearray, path_latency_quality: dict[int, PathLatencyQuality]) -> None:
    for path_id, (source, confidence) in path_latency_quality.items():
        if path_id < 0 or path_id > _u16_max():
            raise ValueError("path latency quality path id out of range")
        output.append(CONTROL_TYPE_PATH_LATENCY_QUALITY)
        _append_u16(output, 4)
        _append_u16(output, path_id)
        output.append(_encode_latency_source(source))
        output.append(_encode_latency_confidence(confidence))


def _encode_path_latency_stats(output: bytearray, path_latency_stats: dict[int, PathLatencyStats]) -> None:
    for path_id, (rtt_us, clock_error_us, tx_jitter_us, rx_jitter_us, tx_p95_us, rx_p95_us) in path_latency_stats.items():
        if path_id < 0 or path_id > _u16_max():
            raise ValueError("path latency stats path id out of range")
        output.append(CONTROL_TYPE_PATH_LATENCY_STATS)
        _append_u16(output, 26)
        _append_u16(output, path_id)
        _append_u32(output, _optional_metric_u32(rtt_us))
        _append_u32(output, _optional_metric_u32(clock_error_us))
        _append_u32(output, _optional_metric_u32(tx_jitter_us))
        _append_u32(output, _optional_metric_u32(rx_jitter_us))
        _append_u32(output, _optional_metric_u32(tx_p95_us))
        _append_u32(output, _optional_metric_u32(rx_p95_us))


def _encode_path_mtu(
    output: bytearray,
    path_mtu: dict[int, tuple[int | None, int | None, int | None, int | None]],
) -> None:
    for path_id, (tx_link_mtu, tx_frame_mtu, rx_link_mtu, rx_frame_mtu) in path_mtu.items():
        if (
            path_id < 0
            or path_id > _u16_max()
            or not _valid_mtu_pair(tx_link_mtu, tx_frame_mtu)
            or not _valid_mtu_pair(rx_link_mtu, rx_frame_mtu)
            or (tx_link_mtu is None and rx_link_mtu is None)
        ):
            raise ValueError("path MTU value out of range")
        output.append(CONTROL_TYPE_PATH_MTU)
        _append_u16(output, 10)
        _append_u16(output, path_id)
        _append_u16(output, _optional_u16(tx_link_mtu))
        _append_u16(output, _optional_u16(tx_frame_mtu))
        _append_u16(output, _optional_u16(rx_link_mtu))
        _append_u16(output, _optional_u16(rx_frame_mtu))


def _encode_path_pressure(output: bytearray, path_pressure: dict[int, PathPressure]) -> None:
    for path_id, pressure in path_pressure.items():
        if path_id < 0 or path_id > _u16_max():
            raise ValueError("path pressure path id out of range")
        (
            loss_ppm,
            queue_depth_packets,
            queue_depth_bytes,
            queue_oldest_age_us,
            send_failures,
            receive_gaps,
            reorder_depth_packets,
            local_drops,
            scheduler_in_flight_packets,
            scheduler_in_flight_bytes,
            scheduler_predicted_delivery_us,
            reorder_buffer_packets,
            reorder_buffer_oldest_age_us,
            observed_packets,
        ) = pressure
        if loss_ppm > 1_000_000:
            raise ValueError("path pressure loss value out of range")
        output.append(CONTROL_TYPE_PATH_PRESSURE)
        _append_u16(output, 58)
        _append_u16(output, path_id)
        _append_u32(output, _counter_u32(loss_ppm))
        _append_u32(output, _counter_u32(queue_depth_packets))
        _append_u32(output, _counter_u32(queue_depth_bytes))
        _append_u32(output, _counter_u32(queue_oldest_age_us))
        _append_u32(output, _counter_u32(send_failures))
        _append_u32(output, _counter_u32(receive_gaps))
        _append_u32(output, _counter_u32(reorder_depth_packets))
        _append_u32(output, _counter_u32(local_drops))
        _append_u32(output, _counter_u32(scheduler_in_flight_packets))
        _append_u32(output, _counter_u32(scheduler_in_flight_bytes))
        _append_u32(output, _counter_u32(scheduler_predicted_delivery_us))
        _append_u32(output, _counter_u32(reorder_buffer_packets))
        _append_u32(output, _counter_u32(reorder_buffer_oldest_age_us))
        _append_u32(output, _counter_u32(observed_packets))


def _encode_scheduler_status(output: bytearray, scheduler_status: SchedulerStatus | None) -> None:
    """Encode this node's local TX scheduler status for peer diagnostics."""
    if scheduler_status is None:
        return
    configured, effective, rust_mode = scheduler_status
    encoded_values = [configured.encode("utf-8"), effective.encode("utf-8"), rust_mode.encode("utf-8")]
    if any(not value or len(value) > 63 for value in encoded_values):
        raise ValueError("scheduler status value out of range")
    output.append(CONTROL_TYPE_SCHEDULER_STATUS)
    _append_u16(output, sum(len(value) for value in encoded_values) + len(encoded_values))
    for value in encoded_values:
        output.append(len(value))
        output.extend(value)


def _encode_data_transmit_samples(output: bytearray, data_transmit_samples: list[DataTransmitSample]) -> None:
    """Encode sparse real-data transmit timing samples; normal data frames stay compact."""
    for path_id, first_sequence, packet_count, transmit_us in data_transmit_samples:
        if (
            path_id < 0
            or path_id > _u16_max()
            or first_sequence < 0
            or first_sequence >= SEQUENCE_SPACE
            or packet_count <= 0
            or packet_count > _u32_max()
            or transmit_us <= 0
            or transmit_us > _u64_max()
        ):
            raise ValueError("data transmit sample value out of range")
        output.append(CONTROL_TYPE_DATA_TRANSMIT_SAMPLE)
        _append_u16(output, 22)
        _append_u16(output, path_id)
        _append_u64(output, first_sequence)
        _append_u32(output, packet_count)
        _append_u64(output, transmit_us)


def _encode_internal_clock_sync(
    output: bytearray,
    path_clock_sync: list[InternalClockSyncMessage],
) -> None:
    for sync in path_clock_sync:
        if sync.path_id < 0 or sync.path_id > _u16_max() or sync.exchange_id <= 0 or sync.origin_us <= 0:
            raise ValueError("internal clock sync value out of range")
        output.append(CONTROL_TYPE_INTERNAL_CLOCK_SYNC)
        _append_u16(output, 35)
        _append_u64(output, sync.exchange_id)
        _append_u16(output, sync.path_id)
        output.append(sync.mode)
        _append_u64(output, sync.origin_us)
        _append_u64(output, _optional_u64(sync.receive_us))
        _append_u64(output, _optional_u64(sync.transmit_us))


def _encode_sink_time(output: bytearray, sink_time: list[SinkTimeMessage]) -> None:
    for message in sink_time:
        if (
            message.path_id < 0
            or message.path_id > _u16_max()
            or message.sink_unix_us <= 0
            or message.sink_internal_us <= 0
            or message.ntp_state not in {0, 1, 2}
        ):
            raise ValueError("sink time value out of range")
        output.append(CONTROL_TYPE_SINK_TIME)
        _append_u16(output, 19)
        _append_u16(output, message.path_id)
        _append_u64(output, message.sink_unix_us)
        _append_u64(output, message.sink_internal_us)
        output.append(message.ntp_state)


def _decode_path_metadata_value(value: bytes) -> tuple[int, str] | None:
    if len(value) < 3:
        return None
    path_id = _read_u16(value, 0)
    name_len = value[2]
    if len(value) != 3 + name_len:
        return None
    try:
        path_name = value[3:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not path_name:
        return None
    return path_id, path_name


def _decode_service_metadata_value(value: bytes) -> tuple[int, str] | None:
    if len(value) < 3:
        return None
    service_id = _read_u16(value, 0)
    name_len = value[2]
    if service_id < USER_SERVICE_ID_START or len(value) != 3 + name_len:
        return None
    try:
        service_name = value[3:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not service_name:
        return None
    return service_id, service_name


def _decode_service_endpoint_assertion_value(value: bytes) -> tuple[int, str] | None:
    if len(value) < 3:
        return None
    service_id = _read_u16(value, 0)
    target_len = value[2]
    if service_id < USER_SERVICE_ID_START or len(value) != 3 + target_len:
        return None
    try:
        target = value[3:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not target:
        return None
    return service_id, target


def _decode_service_disable_value(value: bytes) -> tuple[int, str] | None:
    if len(value) < 3:
        return None
    service_id = _read_u16(value, 0)
    reason_len = value[2]
    if service_id < USER_SERVICE_ID_START or len(value) != 3 + reason_len:
        return None
    try:
        reason = value[3:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not reason:
        return None
    return service_id, reason


def _decode_service_scheduler_policy_value(value: bytes) -> tuple[int, int, int, int, int, int, int] | None:
    if len(value) != 29:
        return None
    service_id = _read_u16(value, 0)
    if service_id < USER_SERVICE_ID_START:
        return None
    path_policy = value[28]
    if path_policy > 2:
        return None
    return (
        service_id,
        _read_u16(value, 2),
        _read_u32(value, 4),
        _read_u64(value, 8),
        _read_u64(value, 16),
        _read_u32(value, 24),
        path_policy,
    )


def _decode_path_capacity_value(value: bytes) -> tuple[int, int | None, int | None] | None:
    if len(value) != 18:
        return None
    return _read_u16(value, 0), _decode_optional_u64(_read_u64(value, 2)), _decode_optional_u64(_read_u64(value, 10))


def _decode_path_latency_value(value: bytes) -> tuple[int, int | None, int | None, int | None, int | None] | None:
    if len(value) != 18:
        return None
    return (
        _read_u16(value, 0),
        _decode_optional_u32(_read_u32(value, 2)),
        _decode_optional_u32(_read_u32(value, 6)),
        _decode_optional_u32(_read_u32(value, 10)),
        _decode_optional_u32(_read_u32(value, 14)),
    )


def _decode_path_latency_quality_value(value: bytes) -> tuple[int, str | None, str | None] | None:
    if len(value) != 4:
        return None
    source = _decode_latency_source(value[2])
    confidence = _decode_latency_confidence(value[3])
    if source is None and value[2] != 0:
        return None
    if confidence is None and value[3] != 0:
        return None
    return _read_u16(value, 0), source, confidence


def _decode_path_latency_stats_value(value: bytes) -> tuple[int, int | None, int | None, int | None, int | None, int | None, int | None] | None:
    if len(value) != 26:
        return None
    return (
        _read_u16(value, 0),
        _decode_optional_u32(_read_u32(value, 2)),
        _decode_optional_u32(_read_u32(value, 6)),
        _decode_optional_u32(_read_u32(value, 10)),
        _decode_optional_u32(_read_u32(value, 14)),
        _decode_optional_u32(_read_u32(value, 18)),
        _decode_optional_u32(_read_u32(value, 22)),
    )


def _decode_path_mtu_value(value: bytes) -> tuple[int, int | None, int | None, int | None, int | None] | None:
    if len(value) != 10:
        return None
    path_id = _read_u16(value, 0)
    tx_link_mtu = _decode_optional_u16(_read_u16(value, 2))
    tx_frame_mtu = _decode_optional_u16(_read_u16(value, 4))
    rx_link_mtu = _decode_optional_u16(_read_u16(value, 6))
    rx_frame_mtu = _decode_optional_u16(_read_u16(value, 8))
    if (
        not _valid_mtu_pair(tx_link_mtu, tx_frame_mtu)
        or not _valid_mtu_pair(rx_link_mtu, rx_frame_mtu)
        or (tx_link_mtu is None and rx_link_mtu is None)
    ):
        return None
    return path_id, tx_link_mtu, tx_frame_mtu, rx_link_mtu, rx_frame_mtu


def _decode_path_pressure_value(
    value: bytes,
) -> (
    tuple[
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
    ]
    | None
):
    if len(value) != 58:
        return None
    loss_ppm = _read_u32(value, 2)
    if loss_ppm > 1_000_000:
        return None
    return (
        _read_u16(value, 0),
        loss_ppm,
        _read_u32(value, 6),
        _read_u32(value, 10),
        _read_u32(value, 14),
        _read_u32(value, 18),
        _read_u32(value, 22),
        _read_u32(value, 26),
        _read_u32(value, 30),
        _read_u32(value, 34),
        _read_u32(value, 38),
        _read_u32(value, 42),
        _read_u32(value, 46),
        _read_u32(value, 50),
        _read_u32(value, 54),
    )


def _decode_scheduler_status_value(value: bytes) -> SchedulerStatus | None:
    """Decode peer TX scheduler status carried for diagnostics only."""
    cursor = 0
    values: list[str] = []
    for _ in range(3):
        if cursor >= len(value):
            return None
        value_len = value[cursor]
        cursor += 1
        if value_len <= 0 or cursor + value_len > len(value):
            return None
        try:
            values.append(value[cursor : cursor + value_len].decode("utf-8"))
        except UnicodeDecodeError:
            return None
        cursor += value_len
    if cursor != len(value):
        return None
    return values[0], values[1], values[2]


def _decode_data_transmit_sample_value(value: bytes) -> DataTransmitSample | None:
    if len(value) != 22:
        return None
    path_id = _read_u16(value, 0)
    first_sequence = _read_u64(value, 2)
    packet_count = _read_u32(value, 10)
    transmit_us = _read_u64(value, 14)
    if packet_count <= 0 or transmit_us <= 0:
        return None
    return path_id, first_sequence, packet_count, transmit_us


def _decode_internal_clock_sync_value(value: bytes) -> InternalClockSyncMessage | None:
    if len(value) != 35:
        return None
    mode = value[10]
    if mode not in {1, 2}:
        return None
    exchange_id = _read_u64(value, 0)
    origin_us = _read_u64(value, 11)
    if exchange_id <= 0 or origin_us <= 0:
        return None
    receive_us = _decode_optional_u64(_read_u64(value, 19))
    transmit_us = _decode_optional_u64(_read_u64(value, 27))
    if mode == 2 and (receive_us is None or transmit_us is None):
        return None
    return InternalClockSyncMessage(
        exchange_id=exchange_id,
        path_id=_read_u16(value, 8),
        mode=mode,
        origin_us=origin_us,
        receive_us=receive_us,
        transmit_us=transmit_us,
    )


def _decode_sink_time_value(value: bytes) -> SinkTimeMessage | None:
    if len(value) != 19:
        return None
    ntp_state = value[18]
    if ntp_state not in {0, 1, 2}:
        return None
    sink_unix_us = _read_u64(value, 2)
    sink_internal_us = _read_u64(value, 10)
    if sink_unix_us <= 0 or sink_internal_us <= 0:
        return None
    return SinkTimeMessage(
        path_id=_read_u16(value, 0),
        sink_unix_us=sink_unix_us,
        sink_internal_us=sink_internal_us,
        ntp_state=ntp_state,
    )


_LATENCY_SOURCE_TO_CODE = {
    None: 0,
    "reply-rtt-half": 1,
    "clock-synced-one-way": 2,
    "data-traffic-one-way": 3,
    "rejected": 4,
    "peer": 5,
}
_LATENCY_SOURCE_BY_CODE = {code: source for source, code in _LATENCY_SOURCE_TO_CODE.items()}
_LATENCY_CONFIDENCE_TO_CODE = {
    None: 0,
    "coarse": 1,
    "warming": 2,
    "good": 3,
    "rejected": 4,
}
_LATENCY_CONFIDENCE_BY_CODE = {code: confidence for confidence, code in _LATENCY_CONFIDENCE_TO_CODE.items()}


def _encode_latency_source(source: str | None) -> int:
    if source not in _LATENCY_SOURCE_TO_CODE:
        raise ValueError("path latency source value out of range")
    return _LATENCY_SOURCE_TO_CODE[source]


def _decode_latency_source(value: int) -> str | None:
    return _LATENCY_SOURCE_BY_CODE.get(value)


def _encode_latency_confidence(confidence: str | None) -> int:
    if confidence not in _LATENCY_CONFIDENCE_TO_CODE:
        raise ValueError("path latency confidence value out of range")
    return _LATENCY_CONFIDENCE_TO_CODE[confidence]


def _decode_latency_confidence(value: int) -> str | None:
    return _LATENCY_CONFIDENCE_BY_CODE.get(value)


def _optional_u64(value: int | None) -> int:
    if value is None:
        return 0
    if value <= 0 or value > _u64_max():
        raise ValueError("path capacity value out of range")
    return value


def _optional_u32(value: int | None) -> int:
    if value is None:
        return 0
    if value <= 0 or value > _u32_max():
        raise ValueError("path latency value out of range")
    return value


def _optional_metric_u32(value: int | None) -> int:
    if value is None:
        return 0
    if value < 0 or value > _u32_max():
        raise ValueError("path metric value out of range")
    return value


def _counter_u32(value: int) -> int:
    if value < 0 or value > _u32_max():
        raise ValueError("path pressure counter value out of range")
    return value


def _optional_u16(value: int | None) -> int:
    if value is None:
        return 0
    if value <= 0 or value > _u16_max():
        raise ValueError("path MTU value out of range")
    return value


def _decode_optional_u64(value: int) -> int | None:
    return value or None


def _decode_optional_u32(value: int) -> int | None:
    return value or None


def _decode_optional_u16(value: int) -> int | None:
    return value or None


def _valid_mtu_pair(link_mtu: int | None, frame_mtu: int | None) -> bool:
    if link_mtu is None and frame_mtu is None:
        return True
    if link_mtu is None or frame_mtu is None:
        return False
    return frame_mtu > 0 and frame_mtu <= link_mtu


def _u16_max() -> int:
    return (1 << 16) - 1


def _u32_max() -> int:
    return (1 << 32) - 1


def _u64_max() -> int:
    return (1 << 64) - 1


def _append_u16(output: bytearray, value: int) -> None:
    output.extend(value.to_bytes(2, "big"))


def _append_u32(output: bytearray, value: int) -> None:
    output.extend(value.to_bytes(4, "big"))


def _append_u64(output: bytearray, value: int) -> None:
    output.extend(value.to_bytes(8, "big"))


def _write_u16(output: bytearray, offset: int, value: int) -> None:
    output[offset : offset + 2] = value.to_bytes(2, "big")


def _write_u64(output: bytearray, offset: int, value: int) -> None:
    output[offset : offset + 8] = value.to_bytes(8, "big")


def _read_u16(input_bytes: bytes, offset: int) -> int:
    return int.from_bytes(input_bytes[offset : offset + 2], "big")


def _read_u32(input_bytes: bytes, offset: int) -> int:
    return int.from_bytes(input_bytes[offset : offset + 4], "big")


def _read_u64(input_bytes: bytes, offset: int) -> int:
    return int.from_bytes(input_bytes[offset : offset + 8], "big")
