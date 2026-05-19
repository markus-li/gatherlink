"""Path MTU detection helpers owned by the Python control plane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gatherlink.platform.debian import default_debian_backend

DEFAULT_SAFE_FRAME_MTU = 1200
MIN_GATHERLINK_FRAME_MTU = 64
V1_BASE_HEADER_LEN = 14
V2_BASE_HEADER_LEN = 13


@dataclass(frozen=True)
class PathMtuObservation:
    """Local TX-side MTU facts for one path interface."""

    link_mtu: int
    configured_frame_mtu: int
    frame_mtu: int
    payload_mtu: int
    status: str
    source: str
    updated_at: str

    def export_dict(self) -> dict[str, int | str]:
        """
        Return the monitor/control-metadata representation.

        A local interface read tells us what this process can transmit on the
        path. RX MTU is learned from peer control metadata and merged later.
        Legacy non-directional keys stay present for a short transition so
        older status readers remain understandable while services restart.
        """
        return {
            "tx_link_mtu": self.link_mtu,
            "tx_configured_frame_mtu": self.configured_frame_mtu,
            "tx_frame_mtu": self.frame_mtu,
            "tx_payload_mtu": self.payload_mtu,
            "link_mtu": self.link_mtu,
            "configured_frame_mtu": self.configured_frame_mtu,
            "frame_mtu": self.frame_mtu,
            "payload_mtu": self.payload_mtu,
            "status": self.status,
            "source": self.source,
            "updated_at": self.updated_at,
        }


def detect_interface_mtu(interface: str, *, sys_class_net: Path = Path("/sys/class/net")) -> int | None:
    """Read a Linux interface MTU from sysfs when the interface is visible in this namespace."""
    return default_debian_backend().read_interface_mtu(interface, sys_class_net=sys_class_net)


def observe_path_mtu(
    interface: str,
    configured_frame_mtu: int,
    *,
    frame_header_len: int = V1_BASE_HEADER_LEN,
) -> PathMtuObservation:
    """
    Return the safe frame MTU Python should advertise and eventually compile.

    Passive detection is intentionally cheap and safe to repeat: it only reads
    the local carrier interface MTU. Active PMTU probes can later use this as a
    starting ceiling, but Rust should never guess above the Python-compiled
    frame MTU.
    """
    link_mtu = detect_interface_mtu(interface) or max(configured_frame_mtu, DEFAULT_SAFE_FRAME_MTU)
    frame_mtu = min(configured_frame_mtu, link_mtu)
    frame_mtu = max(frame_mtu, MIN_GATHERLINK_FRAME_MTU)
    status = "ok" if configured_frame_mtu <= link_mtu else "clamped"
    return PathMtuObservation(
        link_mtu=link_mtu,
        configured_frame_mtu=configured_frame_mtu,
        frame_mtu=frame_mtu,
        payload_mtu=max(frame_mtu - frame_header_len, 0),
        status=status,
        source="interface",
        updated_at=datetime.now(UTC).isoformat(),
    )


def detect_runtime_path_mtu(
    runtime_config: Any,
    *,
    logger: Callable[[str], None] | None = None,
) -> dict[str, dict[str, int | str | None]]:
    """Passively detect each runtime path's local interface MTU for control metadata."""
    observations: dict[str, dict[str, int | str | None]] = {}
    frame_header_len = (
        V2_BASE_HEADER_LEN if getattr(runtime_config.security, "mode", "none") == "static" else V1_BASE_HEADER_LEN
    )
    for path in runtime_config.paths:
        observation = observe_path_mtu(path.interface, path.scheduler.mtu, frame_header_len=frame_header_len)
        observations[path.name] = observation.export_dict()
        if observation.status != "ok":
            _log(
                logger,
                "MTU check "
                f"path={path.name} interface={path.interface} configured={observation.configured_frame_mtu} "
                f"link={observation.link_mtu} active={observation.frame_mtu} status={observation.status}",
            )
    return observations


def _log(logger: Callable[[str], None] | None, message: str) -> None:
    if logger is not None:
        logger(message)
