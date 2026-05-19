"""
time.sources.direct_ntp module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)

NTP_UNIX_EPOCH_DELTA_SECONDS = 2_208_988_800
NTP_QUERY_TIMEOUT_SECONDS = 1.0
NTP_QUERY_PORT = 123
NTP_CLIENT_MODE = 3
NTP_VERSION = 4


@dataclass(frozen=True)
class DirectNtpSample:
    """
    A direct NTP observation suitable for sink-authoritative Gatherlink time.

    The sample stores both the NTP-derived Unix time and the local monotonic
    time when the reply was received. Callers can advance the sample using
    process monotonic time without treating it as permission to set OS time.
    """

    server: str
    unix_us: int
    received_monotonic_us: int
    offset_us: int
    rtt_us: int
    stratum: int | None
    source: str = "ntp"

    def current_unix_us(self) -> int:
        """Return this NTP sample advanced by local monotonic elapsed time."""
        elapsed_us = max(time.monotonic_ns() // 1000 - self.received_monotonic_us, 0)
        return self.unix_us + elapsed_us


def query_direct_ntp(server: str, *, timeout_seconds: float = NTP_QUERY_TIMEOUT_SECONDS) -> DirectNtpSample | None:
    """
    Query one NTP server and return a structured clock sample.

    This is intentionally tiny and dependency-free. Python owns time-source
    selection policy; this helper only performs the unauthenticated UDP NTP
    exchange and reports the measured facts. Production can later add NTS or
    helper-mediated time sources without changing the monitor/status shape.
    """
    packet = bytearray(48)
    packet[0] = (NTP_VERSION << 3) | NTP_CLIENT_MODE
    response = b""
    origin_unix_us = destination_unix_us = destination_monotonic_us = 0
    try:
        addresses = socket.getaddrinfo(server, NTP_QUERY_PORT, type=socket.SOCK_DGRAM)
    except OSError:
        logger.debug("direct NTP name resolution failed", extra={"server": server})
        return None
    for family, socket_type, protocol, _canonical_name, address in addresses:
        try:
            with socket.socket(family, socket_type, protocol) as sock:
                sock.settimeout(timeout_seconds)
                origin_unix_us = time.time_ns() // 1000
                sock.sendto(packet, address)
                response, _address = sock.recvfrom(48)
                destination_monotonic_us = time.monotonic_ns() // 1000
                destination_unix_us = time.time_ns() // 1000
                break
        except (OSError, TimeoutError):
            logger.debug("direct NTP query failed", extra={"server": server})
    if not response:
        return None
    if len(response) < 48:
        logger.debug("direct NTP reply too short", extra={"server": server, "bytes": len(response)})
        return None
    stratum = response[1]
    receive_unix_us = _ntp_timestamp_to_unix_us(response[32:40])
    transmit_unix_us = _ntp_timestamp_to_unix_us(response[40:48])
    if transmit_unix_us <= 0 or stratum == 0:
        logger.debug("direct NTP reply was not usable", extra={"server": server, "stratum": stratum})
        return None
    # Standard four-timestamp NTP calculation using Unix wall time for the local
    # send/receive stamps. The sample's advertised truth is the server transmit
    # time advanced by half the measured round-trip, which is good enough for
    # lab truth-source selection without mutating the host clock.
    rtt_us = max((destination_unix_us - origin_unix_us) - (transmit_unix_us - receive_unix_us), 0)
    offset_us = ((receive_unix_us - origin_unix_us) + (transmit_unix_us - destination_unix_us)) // 2
    unix_us = transmit_unix_us + max(rtt_us // 2, 0)
    return DirectNtpSample(
        server=server,
        unix_us=unix_us,
        received_monotonic_us=destination_monotonic_us,
        offset_us=offset_us,
        rtt_us=rtt_us,
        stratum=stratum,
    )


def _ntp_timestamp_to_unix_us(raw: bytes) -> int:
    seconds, fraction = struct.unpack("!II", raw)
    unix_seconds = seconds - NTP_UNIX_EPOCH_DELTA_SECONDS
    return unix_seconds * 1_000_000 + int(fraction * 1_000_000 / 2**32)
