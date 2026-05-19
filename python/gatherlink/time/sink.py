"""Sink-side time-source selection for Gatherlink control metadata."""

from __future__ import annotations

import json
import os

from gatherlink.platform.debian import default_debian_backend
from gatherlink.time.sources.direct_ntp import DirectNtpSample, query_direct_ntp
from gatherlink.time.sources.https_time import query_https_date_time

# Prefer large, anycast public services before the pool. Cloudflare supports
# NTS for future authenticated time work; Google is broadly reachable but uses
# leap smear, so production policy should avoid mixing smear and non-smear
# sources for tight correction during leap-second windows.
SINK_NTP_SERVERS = ("time.cloudflare.com", "time.google.com", "pool.ntp.org")
SINK_HTTPS_TIME_URLS = ("https://www.cloudflare.com/cdn-cgi/trace", "https://www.google.com/generate_204")
SINK_TIME_BOOTSTRAP_ENV = "GATHERLINK_SINK_TIME_SAMPLE_JSON"


def read_sink_ntp_sample(
    servers: tuple[str, ...] = SINK_NTP_SERVERS,
    https_urls: tuple[str, ...] = SINK_HTTPS_TIME_URLS,
) -> DirectNtpSample | None:
    """
    Return the sink-side wall-clock source of truth when direct NTP is reachable.

    The sink is authoritative for Gatherlink time, but it should derive that
    wall-clock truth from NTP when possible. If UDP NTP is blocked, HTTPS Date
    headers provide a lower-confidence fallback that stays outside the
    Gatherlink connection. This function never steps system time; it only
    selects a structured sample for control metadata.
    """
    for server in servers:
        sample = query_direct_ntp(server)
        if sample is not None:
            return sample
    for url in https_urls:
        sample = query_https_date_time(url)
        if sample is not None:
            return sample
    sample = read_bootstrap_sink_time_sample()
    if sample is not None:
        return sample
    return None


def encode_sink_time_sample(sample: DirectNtpSample) -> str:
    """Serialize a sink time sample for lab namespace bootstrap."""
    return json.dumps(
        {
            "server": sample.server,
            "unix_us": sample.unix_us,
            "received_monotonic_us": sample.received_monotonic_us,
            "offset_us": sample.offset_us,
            "rtt_us": sample.rtt_us,
            "stratum": sample.stratum,
            "source": sample.source,
        },
        sort_keys=True,
    )


def read_bootstrap_sink_time_sample() -> DirectNtpSample | None:
    """
    Read a parent-provided sink time sample.

    Local labs run the sink inside an isolated network namespace that usually
    has no route to UDP/123 or HTTPS. The launcher can sample time before
    entering that namespace and preserve the result with ``sudo -E``. Production
    services should prefer direct external time and only use this when their
    supervisor deliberately provides it.
    """
    raw = os.environ.get(SINK_TIME_BOOTSTRAP_ENV)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return DirectNtpSample(
            server=str(payload["server"]),
            unix_us=int(payload["unix_us"]),
            received_monotonic_us=int(payload["received_monotonic_us"]),
            offset_us=int(payload["offset_us"]),
            rtt_us=int(payload["rtt_us"]),
            stratum=int(payload["stratum"]) if payload.get("stratum") is not None else None,
            source=str(payload.get("source") or "bootstrap"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def read_system_ntp_status() -> str:
    """Read system NTP synchronization status without attempting to set time."""
    return default_debian_backend().ntp_synchronization_state()
