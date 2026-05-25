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
    carrier_max_datagram_size: int | None = None

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
            "carrier_max_datagram_size": self.carrier_max_datagram_size,
            "status": self.status,
            "source": self.source,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PathMtuDowngradeRecommendation:
    """Python-owned recommendation from safe MTU ceilings or runtime symptoms."""

    path_name: str
    current_frame_mtu: int
    recommended_frame_mtu: int
    trigger: str
    reason: str

    @property
    def changed(self) -> bool:
        """Return whether the recommendation would lower the active frame MTU."""
        return self.recommended_frame_mtu < self.current_frame_mtu

    def export_dict(self) -> dict[str, int | str | bool]:
        """Return stable facts for diagnostics, docs, and tests."""
        return {
            "path": self.path_name,
            "current_frame_mtu": self.current_frame_mtu,
            "recommended_frame_mtu": self.recommended_frame_mtu,
            "trigger": self.trigger,
            "reason": self.reason,
            "changed": self.changed,
        }


def detect_interface_mtu(interface: str, *, sys_class_net: Path = Path("/sys/class/net")) -> int | None:
    """Read a Linux interface MTU from sysfs when the interface is visible in this namespace."""
    return default_debian_backend().read_interface_mtu(interface, sys_class_net=sys_class_net)


def observe_path_mtu(
    interface: str,
    configured_frame_mtu: int,
    *,
    frame_header_len: int = V1_BASE_HEADER_LEN,
    carrier_max_datagram_size: int | None = None,
) -> PathMtuObservation:
    """
    Return the safe frame MTU Python should advertise and eventually compile.

    Passive detection is intentionally cheap and safe to repeat: it only reads
    the local carrier interface MTU. Active PMTU probes can later use this as a
    starting ceiling, but Rust should never guess above the Python-compiled
    frame MTU.
    """
    link_mtu = detect_interface_mtu(interface) or max(configured_frame_mtu, DEFAULT_SAFE_FRAME_MTU)
    ceilings = [configured_frame_mtu, link_mtu]
    if carrier_max_datagram_size is not None:
        ceilings.append(carrier_max_datagram_size)
    frame_mtu = min(ceilings)
    frame_mtu = max(frame_mtu, MIN_GATHERLINK_FRAME_MTU)
    status = "ok" if configured_frame_mtu == frame_mtu else "clamped"
    return PathMtuObservation(
        link_mtu=link_mtu,
        configured_frame_mtu=configured_frame_mtu,
        frame_mtu=frame_mtu,
        payload_mtu=max(frame_mtu - frame_header_len, 0),
        status=status,
        source="interface",
        updated_at=datetime.now(UTC).isoformat(),
        carrier_max_datagram_size=carrier_max_datagram_size,
    )


def recommend_path_mtu_downgrade(
    path_name: str,
    *,
    current_frame_mtu: int,
    path_status: dict[str, object],
    carrier_max_datagram_size: int | None = None,
) -> PathMtuDowngradeRecommendation | None:
    """
    Recommend a conservative MTU downgrade from explicit safe facts.

    Python owns this policy because it interprets runtime symptoms. Rust should
    only receive the resulting frame MTU after Python decides to hot-reapply it.
    The first v0.9.2 behavior is deliberately conservative: carrier ceilings and
    explicit too-large/fragmentation symptoms can lower MTU, but generic packet
    loss alone cannot.
    """
    if carrier_max_datagram_size is not None and carrier_max_datagram_size < current_frame_mtu:
        return PathMtuDowngradeRecommendation(
            path_name=path_name,
            current_frame_mtu=current_frame_mtu,
            recommended_frame_mtu=max(MIN_GATHERLINK_FRAME_MTU, carrier_max_datagram_size),
            trigger="carrier_max_datagram_size",
            reason="carrier max datagram size is below current frame MTU",
        )
    too_large_packets = _status_int(path_status.get("packet_too_large_packets")) + _status_int(
        path_status.get("datagram_too_large_packets")
    )
    fragmentation_failures = _status_int(path_status.get("fragmentation_failed_packets"))
    if too_large_packets or fragmentation_failures:
        decrement = max(80, current_frame_mtu // 16)
        return PathMtuDowngradeRecommendation(
            path_name=path_name,
            current_frame_mtu=current_frame_mtu,
            recommended_frame_mtu=max(MIN_GATHERLINK_FRAME_MTU, current_frame_mtu - decrement),
            trigger="too_large_or_fragmentation_failed",
            reason="explicit too-large or fragmentation-failed counters increased",
        )
    return None


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
        observation = observe_path_mtu(
            path.interface,
            path.scheduler.mtu,
            frame_header_len=frame_header_len,
            carrier_max_datagram_size=path.carrier_max_datagram_size,
        )
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


def _status_int(value: object) -> int:
    """Convert loose status counters to non-negative integers for MTU policy."""
    if value is None:
        return 0
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, converted)
