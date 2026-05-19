"""Process launch planning for Python-owned helper services."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from gatherlink.config.runtime import (
    RuntimeConfig,
    RuntimeDnsHelperConfig,
    RuntimeHelperConfig,
    RuntimeSocks5HelperConfig,
    RuntimeTcpForwardHelperConfig,
)
from gatherlink.runtime.services import service_name


@dataclass(frozen=True)
class HelperLaunchPlan:
    """One process-managed helper launch command derived from runtime config."""

    name: str
    kind: str
    command: list[str]
    log_file: Path
    diagnostics_jsonl: Path | None
    metadata: dict[str, str]


def build_helper_launch_plans(
    runtime_config: RuntimeConfig,
    *,
    registry_dir: Path,
    name_prefix: str | None = None,
) -> list[HelperLaunchPlan]:
    """
    Build helper process commands without starting them.

    Helper behavior stays in helper modules and CLIs. This compiler only turns
    explicit runtime helper facts into process commands and registry metadata.
    """
    plans: list[HelperLaunchPlan] = []
    for helper in runtime_config.helpers:
        if not helper.enabled:
            continue
        command = _helper_command(helper)
        if command is None:
            continue
        base_name = name_prefix or service_name("helper", f"{runtime_config.node}.{helper.kind}")
        if sum(1 for existing in plans if existing.kind == helper.kind) > 0:
            base_name = f"{base_name}.{len(plans) + 1}"
        service_dir = registry_dir / base_name
        diagnostics_jsonl = service_dir / "diagnostics.jsonl"
        plans.append(
            HelperLaunchPlan(
                name=base_name,
                kind=f"helper:{helper.kind}",
                command=[*command, "--diagnostics-jsonl", str(diagnostics_jsonl)]
                if _supports_diagnostics(helper)
                else command,
                log_file=service_dir / "service.log",
                diagnostics_jsonl=diagnostics_jsonl if _supports_diagnostics(helper) else None,
                metadata=_helper_metadata(runtime_config, helper),
            )
        )
    return plans


def _helper_command(helper: RuntimeHelperConfig) -> list[str] | None:
    """Return the CLI command for helpers that are startable in v1."""
    base = [sys.executable, "-m", "gatherlink.cli.main", "helpers"]
    if isinstance(helper, RuntimeDnsHelperConfig):
        return [*base, "dns-serve", "--listen", helper.listen]
    if isinstance(helper, RuntimeSocks5HelperConfig):
        if not helper.service_listen:
            raise ValueError(f"socks5 helper service {helper.service} must have a local listen endpoint")
        command = [*base, "socks5-serve", "--listen", helper.listen, "--gatherlink-service", helper.service_listen]
        for host in helper.allow_hosts:
            command.extend(["--allow-host", host])
        for port in helper.allow_ports:
            command.extend(["--allow-port", str(port)])
        return command
    if isinstance(helper, RuntimeTcpForwardHelperConfig):
        if not helper.service_listen:
            raise ValueError(f"tcp_forward helper service {helper.service} must have a local listen endpoint")
        return [
            *base,
            "tcp-forward",
            "--listen",
            helper.listen,
            "--target",
            helper.target,
            "--gatherlink-service",
            helper.service_listen,
            "--connect-timeout",
            str(helper.connect_timeout_seconds),
            "--idle-timeout",
            str(helper.idle_timeout_seconds),
        ]
    # WireGuard is currently planning/config guidance rather than a long-running
    # helper process. Keep that explicit here until its lifecycle is finalized.
    return None


def _supports_diagnostics(helper: RuntimeHelperConfig) -> bool:
    """Return whether the helper CLI supports the shared JSONL diagnostics flag."""
    return isinstance(helper, RuntimeSocks5HelperConfig | RuntimeTcpForwardHelperConfig)


def _helper_metadata(runtime_config: RuntimeConfig, helper: RuntimeHelperConfig) -> dict[str, str]:
    """Return operator-safe registry metadata for one helper process."""
    metadata = {"node": runtime_config.node, "helper_kind": helper.kind}
    service_name_value = getattr(helper, "service", None)
    if service_name_value:
        metadata["service"] = str(service_name_value)
    service_listen = getattr(helper, "service_listen", None)
    if service_listen:
        metadata["gatherlink_service"] = str(service_listen)
    return metadata
