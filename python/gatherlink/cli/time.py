"""
cli.time module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from gatherlink.shared.logging import get_logger
from gatherlink.time.helper_client import (
    DEFAULT_MAX_STEP_US,
    DEFAULT_TIME_HELPER_SOCKET,
    TimeCorrectionRequest,
    request_time_correction,
)
from gatherlink.time.sink import read_sink_ntp_sample, read_system_ntp_status

logger = get_logger(__name__)

app = typer.Typer(help="Inspect Gatherlink time state and talk to the privileged time helper.")


@app.command("status")
def status() -> None:
    """Show the current sink-side time source that Gatherlink would advertise."""
    sample = read_sink_ntp_sample()
    payload: dict[str, object] = {"system_ntp": read_system_ntp_status()}
    if sample is None:
        payload["source"] = None
        payload["warning"] = "no direct NTP, HTTPS Date, or bootstrap sink-time sample was available"
    else:
        payload["source"] = {
            "server": sample.server,
            "source_type": sample.source,
            "current_unix_us": sample.current_unix_us(),
            "offset_us": sample.offset_us,
            "rtt_us": sample.rtt_us,
            "stratum": sample.stratum,
        }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("correct")
def correct(
    target_unix_us: int = typer.Argument(..., help="Target Unix wall time in microseconds."),
    socket_path: Path = typer.Option(
        DEFAULT_TIME_HELPER_SOCKET,
        "--socket",
        help="Unix socket exposed by gatherlink-time-helper.",
    ),
    source: str = typer.Option("manual", "--source", help="Diagnostic source name for this correction."),
    quality: str = typer.Option("operator-approved", "--quality", help="Diagnostic quality label."),
    max_step_us: int = typer.Option(
        DEFAULT_MAX_STEP_US,
        "--max-step-us",
        help="Refuse if the requested correction is larger than this many microseconds.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually set system time. Without this flag the helper returns a preview only.",
    ),
) -> None:
    """
    Ask the privileged helper to preview or apply a bounded system-time correction.

    Normal hosts should use chrony, systemd-timesyncd, ntpd, or appliance time
    services. This command exists for explicit deployments where Gatherlink is
    the chosen time authority bridge.
    """
    if apply:
        typer.echo(
            "WARNING: applying system time through Gatherlink. Ensure normal NTP agents are disabled or coordinated.",
            err=True,
        )
    request = TimeCorrectionRequest(
        target_unix_us=target_unix_us,
        source=source,
        quality=quality,
        max_step_us=max_step_us,
        apply=apply,
    )
    response = request_time_correction(request, socket_path=socket_path)
    typer.echo(json.dumps(response.export_dict(), indent=2, sort_keys=True))
    if response.status in {"error", "refused"}:
        raise typer.Exit(1)
