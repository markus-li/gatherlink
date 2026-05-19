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
GATHERLINK_KIND_DATA = 1
GATHERLINK_KIND_CONTROL = 2
GATHERLINK_V1_HEADER_LEN = 44
CONTROL_PAYLOAD_VERSION = 1
CONTROL_TYPE_PATH_METADATA = 3
CONTROL_TYPE_PATH_CAPACITY = 4
CONTROL_TYPE_PATH_LATENCY = 5
CONTROL_TYPE_INTERNAL_CLOCK_SYNC = 6
CONTROL_TYPE_SINK_TIME = 7
SEQUENCE_SPACE = 1 << 64
DEFAULT_SESSION_ID = 0
DEFAULT_SERVICE_ID = 0
DEFAULT_ROUTE_ID = 0


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
    path_capacity_bps: dict[int, tuple[int | None, int | None]]
    path_latency_us: dict[int, tuple[int | None, int | None, int | None, int | None]]
    internal_clock_sync: list[InternalClockSyncMessage]
    sink_time: list[SinkTimeMessage]


def encode_data_frame(sequence: int, path_id: int, payload: bytes) -> bytes:
    """
    Encapsulate normal UDP traffic in the same fixed v1 frame shape Rust defines.

    The user packet is untouched inside the payload. Sequence, path id, and route metadata live in the Gatherlink path
    frame, which is the layer where real loss and reordering telemetry belongs.
    """
    if sequence < 0 or sequence >= SEQUENCE_SPACE or path_id < 0 or path_id > _u16_max():
        raise ValueError("protocol data frame metadata value out of range")
    if len(payload) > _u16_max():
        raise ValueError("protocol data frame payload is too large")

    header = bytearray(GATHERLINK_V1_HEADER_LEN)
    header[0] = GATHERLINK_PROTOCOL_VERSION
    header[1] = GATHERLINK_KIND_DATA
    _write_u16(header, 2, GATHERLINK_V1_HEADER_LEN)
    _write_u16(header, 4, 0)
    _write_u128(header, 6, DEFAULT_SESSION_ID)
    _write_u64(header, 22, DEFAULT_SERVICE_ID)
    _write_u16(header, 30, path_id)
    _write_u16(header, 32, DEFAULT_ROUTE_ID)
    _write_u64(header, 34, sequence)
    _write_u16(header, 42, len(payload))
    return bytes(header) + payload


def encode_control_frame(path_id: int, payload: bytes) -> bytes:
    """Encode a v1 control frame for sparse peer metadata."""
    if path_id < 0 or path_id > _u16_max() or len(payload) > _u16_max():
        raise ValueError("protocol control frame value out of range")
    header = bytearray(GATHERLINK_V1_HEADER_LEN)
    header[0] = GATHERLINK_PROTOCOL_VERSION
    header[1] = GATHERLINK_KIND_CONTROL
    _write_u16(header, 2, GATHERLINK_V1_HEADER_LEN)
    _write_u16(header, 4, 0)
    _write_u128(header, 6, DEFAULT_SESSION_ID)
    _write_u64(header, 22, DEFAULT_SERVICE_ID)
    _write_u16(header, 30, path_id)
    _write_u16(header, 32, DEFAULT_ROUTE_ID)
    _write_u64(header, 34, 0)
    _write_u16(header, 42, len(payload))
    return bytes(header) + payload


def decode_data_frame(payload: bytes) -> DataFrame | None:
    """Decode one v1 data frame and return the original UDP payload."""
    if len(payload) < GATHERLINK_V1_HEADER_LEN:
        return None
    if payload[0] != GATHERLINK_PROTOCOL_VERSION or payload[1] != GATHERLINK_KIND_DATA:
        return None

    header_len = _read_u16(payload, 2)
    payload_len = _read_u16(payload, 42)
    if header_len < GATHERLINK_V1_HEADER_LEN or len(payload) != header_len + payload_len:
        return None

    return DataFrame(
        path_id=_read_u16(payload, 30),
        sequence=_read_u64(payload, 34),
        payload=payload[header_len:],
    )


def decode_control_frame(payload: bytes) -> ControlFrame | None:
    """Decode the Python-supported subset of the v1 control metaband."""
    if len(payload) < GATHERLINK_V1_HEADER_LEN:
        return None
    if payload[0] != GATHERLINK_PROTOCOL_VERSION or payload[1] != GATHERLINK_KIND_CONTROL:
        return None

    header_len = _read_u16(payload, 2)
    payload_len = _read_u16(payload, 42)
    if header_len < GATHERLINK_V1_HEADER_LEN or len(payload) != header_len + payload_len:
        return None

    return decode_control_payload(payload[header_len:])


def encode_control_payload(
    path_metadata: dict[int, str],
    path_capacity_bps: dict[int, tuple[int | None, int | None]] | None = None,
    path_latency_us: dict[int, tuple[int | None, int | None, int | None, int | None]] | None = None,
    path_clock_sync: list[InternalClockSyncMessage] | None = None,
    sink_time: list[SinkTimeMessage] | None = None,
) -> bytes:
    """Encode optional control messages; absent fields cost no bytes on the wire."""
    path_capacity_bps = path_capacity_bps or {}
    path_latency_us = path_latency_us or {}
    path_clock_sync = path_clock_sync or []
    sink_time = sink_time or []
    message_count = (
        len(path_metadata) + len(path_capacity_bps) + len(path_latency_us) + len(path_clock_sync) + len(sink_time)
    )
    if message_count == 0:
        raise ValueError("control payload must include at least one message")
    output = bytearray()
    output.append(CONTROL_PAYLOAD_VERSION)
    _append_u16(output, message_count)
    _encode_path_metadata(output, path_metadata)
    _encode_path_capacity(output, path_capacity_bps)
    _encode_path_latency(output, path_latency_us)
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
    path_capacity_bps: dict[int, tuple[int | None, int | None]] = {}
    path_latency_us: dict[int, tuple[int | None, int | None, int | None, int | None]] = {}
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
        path_capacity_bps=path_capacity_bps,
        path_latency_us=path_latency_us,
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


def _decode_optional_u64(value: int) -> int | None:
    return value or None


def _decode_optional_u32(value: int) -> int | None:
    return value or None


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


def _write_u128(output: bytearray, offset: int, value: int) -> None:
    output[offset : offset + 16] = value.to_bytes(16, "big")


def _read_u16(input_bytes: bytes, offset: int) -> int:
    return int.from_bytes(input_bytes[offset : offset + 2], "big")


def _read_u32(input_bytes: bytes, offset: int) -> int:
    return int.from_bytes(input_bytes[offset : offset + 4], "big")


def _read_u64(input_bytes: bytes, offset: int) -> int:
    return int.from_bytes(input_bytes[offset : offset + 8], "big")
