"""
Client for the privileged time-helper mini-service over a Unix socket.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import socket
from pathlib import Path

from gatherlink.shared.logging import get_logger
from gatherlink.shared.models import GatherlinkBaseModel

logger = get_logger(__name__)

DEFAULT_TIME_HELPER_SOCKET = Path("/run/gatherlink/time-helper.sock")
DEFAULT_MAX_STEP_US = 500_000


class TimeCorrectionRequest(GatherlinkBaseModel):
    """
    Narrow request sent to the privileged time helper.

    Python owns source selection and policy. The helper receives only the final
    target wall time, a bound, and whether the operator explicitly requested
    application instead of preview.
    """

    target_unix_us: int
    source: str = "unknown"
    quality: str = "unknown"
    max_step_us: int = DEFAULT_MAX_STEP_US
    apply: bool = False

    def render_wire(self) -> str:
        """Render the tiny line-oriented local helper protocol."""
        return (
            f"target_unix_us={self.target_unix_us}\n"
            f"source={self.source}\n"
            f"quality={self.quality}\n"
            f"max_step_us={self.max_step_us}\n"
            f"apply={str(self.apply).lower()}\n"
        )


class TimeCorrectionResponse(GatherlinkBaseModel):
    """Structured response returned by the privileged time helper."""

    status: str
    applied: bool
    offset_us: int
    target_unix_us: int
    system_unix_us: int
    warning: str | None = None


def request_time_correction(
    request: TimeCorrectionRequest,
    *,
    socket_path: Path = DEFAULT_TIME_HELPER_SOCKET,
    timeout_seconds: float = 2.0,
) -> TimeCorrectionResponse:
    """Send a time correction request to the local privileged helper."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout_seconds)
        client.connect(str(socket_path))
        client.sendall(request.render_wire().encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        raw = _recv_all(client).decode("utf-8")
    return parse_time_helper_response(raw)


def parse_time_helper_response(raw: str) -> TimeCorrectionResponse:
    """Parse the helper response into structured diagnostics."""
    fields: dict[str, str] = {}
    for raw_line in raw.splitlines():
        if not raw_line:
            continue
        key, separator, value = raw_line.partition("=")
        if not separator:
            raise ValueError(f"invalid time-helper response line: {raw_line}")
        fields[key] = value
    return TimeCorrectionResponse(
        status=fields.get("status", "error"),
        applied=fields.get("applied") == "true",
        offset_us=int(fields.get("offset_us", "0")),
        target_unix_us=int(fields.get("target_unix_us", "0")),
        system_unix_us=int(fields.get("system_unix_us", "0")),
        warning=fields.get("warning"),
    )


def _recv_all(client: socket.socket) -> bytes:
    chunks = []
    while True:
        chunk = client.recv(4096)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)

