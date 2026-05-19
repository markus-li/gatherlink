"""
Linux namespace/veth/tc lab setup helpers.

This module owns the root-capable lab plumbing: network namespaces, veth pairs,
interface state, MTU, and qdisc shaping. The Gatherlink services started inside
those namespaces still run unprivileged; only this lab setup layer is allowed to
touch system networking.
"""

from __future__ import annotations

import ipaddress
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from gatherlink.lab.scenarios import (
    LabNetworkModeConfig,
    LabPathConfig,
    LabScenarioConfig,
    LabShapeConfig,
    LabShapeProfileConfig,
    LabShapeSide,
)
from gatherlink.platform.debian import CommandRunner, SubprocessCommandRunner, default_debian_backend


class SubprocessRunner(SubprocessCommandRunner):
    """Run lab setup commands through subprocess."""


@dataclass(frozen=True)
class PathSetupResult:
    """Result for one simulated path setup operation."""

    name: str
    status: str
    client_namespace: str
    server_namespace: str
    client_interface: str
    server_interface: str
    shape_actions: list[str]


@dataclass(frozen=True)
class ShapeApplyResult:
    """Result from applying live link shaping to one path."""

    name: str
    side: str
    client_namespace: str
    server_namespace: str
    client_interface: str
    server_interface: str
    actions: list[str]


@dataclass(frozen=True)
class LabCleanupResult:
    """Result from removing one lab-owned network namespace."""

    namespace: str
    status: str
    action: str = "delete_namespace"


def prepare_lab_runtime(config: LabScenarioConfig, *, runner: CommandRunner | None = None) -> list[PathSetupResult]:
    """Prepare root-owned simulated network paths for a lab scenario."""
    runner = runner or SubprocessRunner()
    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "scenario.json").write_text(
        json.dumps(config.export_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )

    results: list[PathSetupResult] = []
    for path in config.paths:
        result = _ensure_path(config, path, runner=runner)
        if path.shape != LabShapeConfig():
            shape_result = apply_lab_shape(config, path.name, path.shape, runner=runner)
            result = PathSetupResult(
                name=result.name,
                status=result.status,
                client_namespace=result.client_namespace,
                server_namespace=result.server_namespace,
                client_interface=result.client_interface,
                server_interface=result.server_interface,
                shape_actions=shape_result.actions,
            )
        results.append(result)
    return results


def apply_lab_profile(
    config: LabScenarioConfig,
    profile_name: str,
    *,
    runner: CommandRunner | None = None,
) -> list[ShapeApplyResult]:
    """Apply a named live shaping profile to existing lab paths."""
    if profile_name not in config.profiles:
        raise ValueError(f"unknown lab profile: {profile_name}")
    runner = runner or SubprocessRunner()
    return [
        apply_lab_shape(config, path_name, shape, side="both", runner=runner)
        for path_name, shape in config.profiles[profile_name].items()
    ]


def apply_lab_shape_profile(
    config: LabScenarioConfig,
    profile: LabShapeProfileConfig,
    *,
    runner: CommandRunner | None = None,
) -> list[ShapeApplyResult]:
    """Apply a standalone shaping config to an existing lab."""
    runner = runner or SubprocessRunner()
    results: list[ShapeApplyResult] = []
    for target in profile.targets:
        if target.clear:
            results.append(clear_lab_shape(config, target.path, side=target.side, runner=runner))
        else:
            results.append(apply_lab_shape(config, target.path, target.shape, side=target.side, runner=runner))
    return results


def apply_lab_network_mode(
    config: LabScenarioConfig,
    mode_name: str,
    *,
    runner: CommandRunner | None = None,
) -> list[ShapeApplyResult]:
    """Apply a named network mode to an existing lab."""
    if mode_name not in config.network_modes:
        raise ValueError(f"unknown lab network mode: {mode_name}")
    return apply_lab_network_mode_config(config, config.network_modes[mode_name], runner=runner)


def apply_lab_network_mode_config(
    config: LabScenarioConfig,
    mode: LabNetworkModeConfig,
    *,
    runner: CommandRunner | None = None,
) -> list[ShapeApplyResult]:
    """Apply one structured network mode definition."""
    runner = runner or SubprocessRunner()
    results: list[ShapeApplyResult] = []
    for target in mode.targets:
        if target.clear:
            results.append(clear_lab_shape(config, target.path, side=target.side, runner=runner))
        else:
            results.append(apply_lab_shape(config, target.path, target.shape, side=target.side, runner=runner))
    return results


def apply_lab_sink_view_rates(
    config: LabScenarioConfig,
    path_name: str,
    *,
    sink_up_rate: str,
    sink_down_rate: str,
    runner: CommandRunner | None = None,
) -> list[ShapeApplyResult]:
    """
    Apply asymmetric rates using sink-side up/down wording.

    The lab shapes egress interfaces. From the sink side, "down" is client/forwarder -> sink traffic, so it belongs
    on the local/client veth egress. "Up" is sink -> client/forwarder reply traffic, so it belongs on the
    remote/server veth egress. Keeping this translation here avoids each test needing to remember the namespace map.
    """
    runner = runner or SubprocessRunner()
    return [
        apply_lab_shape(config, path_name, LabShapeConfig(rate=sink_down_rate), side="local", runner=runner),
        apply_lab_shape(config, path_name, LabShapeConfig(rate=sink_up_rate), side="remote", runner=runner),
    ]


def apply_lab_shape(
    config: LabScenarioConfig,
    path_name: str,
    shape: LabShapeConfig,
    *,
    side: LabShapeSide = "both",
    runner: CommandRunner | None = None,
) -> ShapeApplyResult:
    """Apply live MTU, link state, and traffic shaping to an existing path."""
    runner = runner or SubprocessRunner()
    path = path_by_name(config, path_name)
    handle = path_handle(config, path)
    actions: list[str] = []

    if shape.mtu is not None:
        for namespace, interface in handle.interfaces_for_side(side):
            sudo_ip(["-n", namespace, "link", "set", interface, "mtu", str(shape.mtu)], runner=runner)
        actions.append(f"mtu={shape.mtu}")

    if shape.state is not None:
        for namespace, interface in handle.interfaces_for_side(side):
            sudo_ip(["-n", namespace, "link", "set", interface, shape.state], runner=runner)
        actions.append(f"state={shape.state}")

    if shape.blackhole:
        for namespace, interface in handle.interfaces_for_side(side):
            sudo_tc(
                ["-n", namespace, "qdisc", "replace", "dev", interface, "root", "netem", "loss", "100%"],
                runner=runner,
            )
        actions.append("blackhole=true")
    elif has_netem_shape(shape):
        netem_args = netem_args_for_shape(shape)
        for namespace, interface in handle.interfaces_for_side(side):
            sudo_tc(
                ["-n", namespace, "qdisc", "replace", "dev", interface, "root", "netem", *netem_args],
                runner=runner,
            )
        actions.append("tc=netem")

    if not actions:
        actions.append("no_change")

    return ShapeApplyResult(
        name=path.name,
        side=side,
        client_namespace=handle.client_namespace,
        server_namespace=handle.server_namespace,
        client_interface=handle.client_interface,
        server_interface=handle.server_interface,
        actions=actions,
    )


def clear_lab_shape(
    config: LabScenarioConfig,
    path_name: str,
    *,
    side: LabShapeSide = "both",
    runner: CommandRunner | None = None,
) -> ShapeApplyResult:
    """Clear live qdisc shaping and bring a path back up."""
    runner = runner or SubprocessRunner()
    path = path_by_name(config, path_name)
    handle = path_handle(config, path)
    for namespace, interface in handle.interfaces_for_side(side):
        sudo_tc(["-n", namespace, "qdisc", "del", "dev", interface, "root"], runner=runner, check=False)
        sudo_ip(["-n", namespace, "link", "set", interface, "up"], runner=runner, check=False)
    return ShapeApplyResult(
        name=path.name,
        side=side,
        client_namespace=handle.client_namespace,
        server_namespace=handle.server_namespace,
        client_interface=handle.client_interface,
        server_interface=handle.server_interface,
        actions=["clear_qdisc", "state=up"],
    )


def cleanup_lab_runtime(config: LabScenarioConfig, *, runner: CommandRunner | None = None) -> list[LabCleanupResult]:
    """Remove lab-owned network namespaces and the veth interfaces inside them."""
    runner = runner or SubprocessRunner()
    results: list[LabCleanupResult] = []
    seen_namespaces: set[str] = set()

    for path in config.paths:
        handle = path_handle(config, path)
        for namespace in [handle.client_namespace, handle.server_namespace]:
            if namespace in seen_namespaces:
                continue
            seen_namespaces.add(namespace)
            # TODO(cleanup-scope): Keep cleanup intentionally namespace-focused. Deleting the namespace removes the
            # veth interfaces, qdiscs, addresses, and link state created under it while preserving logs for debugging.
            result = sudo_ip(["netns", "del", namespace], runner=runner, check=False)
            status = "removed" if result.returncode == 0 else "absent_or_already_removed"
            results.append(LabCleanupResult(namespace=namespace, status=status))

    return results


def inspect_lab_interfaces(config: LabScenarioConfig, *, runner: CommandRunner | None = None) -> list[str]:
    """Return `ip addr show` output for each lab namespace interface."""
    runner = runner or SubprocessRunner()
    outputs: list[str] = []
    for path in config.paths:
        handle = path_handle(config, path)
        for namespace, interface in handle.namespace_interfaces:
            result = sudo_ip(["-n", namespace, "addr", "show", "dev", interface], runner=runner)
            outputs.append(f"# {path.name}: {namespace}/{interface}\n{result.stdout.strip()}")
    return outputs


def namespace_exists(namespace: str) -> bool:
    """Return whether a Linux network namespace exists."""
    return default_debian_backend().namespace_exists(namespace)


def client_namespace(config: LabScenarioConfig) -> str:
    """Return the local/client namespace name for a lab scenario."""
    return f"glab-{config.name}-client"


def server_namespace(config: LabScenarioConfig) -> str:
    """Return the remote/server namespace name for a lab scenario."""
    return f"glab-{config.name}-server"


def path_by_name(config: LabScenarioConfig, path_name: str) -> LabPathConfig:
    """Find a configured lab path by name."""
    for path in config.paths:
        if path.name == path_name:
            return path
    raise ValueError(f"unknown lab path: {path_name}")


def path_handle(config: LabScenarioConfig, path: LabPathConfig) -> PathHandle:
    """Return namespace and interface names for one configured path."""
    safe_path = path.name.replace("_", "-")[:6]
    return PathHandle(
        client_namespace=client_namespace(config),
        server_namespace=server_namespace(config),
        client_interface=f"gl{safe_path}c"[:15],
        server_interface=f"gl{safe_path}s"[:15],
    )


def lab_qdisc_stats(config: LabScenarioConfig, *, side: LabShapeSide) -> dict[str, dict[str, int]]:
    """Read live `tc -s qdisc` counters for each lab path in the current namespace."""
    stats: dict[str, dict[str, int]] = {}
    backend = default_debian_backend()
    for path in config.paths:
        handle = path_handle(config, path)
        interface = handle.client_interface if side == "local" else handle.server_interface
        result = backend.qdisc_stats(interface)
        if result.returncode == 0:
            stats[path.name] = parse_tc_qdisc_stats(result.stdout)
    return stats


def qdisc_delta(
    current: dict[str, dict[str, int]],
    baseline: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Return qdisc counters relative to a service-start baseline."""
    delta: dict[str, dict[str, int]] = {}
    for path_name, stats in current.items():
        baseline_row = baseline.get(path_name, {})
        delta[path_name] = {}
        for key, value in stats.items():
            if key == "rate_bps":
                delta[path_name][key] = value
            else:
                delta[path_name][key] = max(value - baseline_row.get(key, 0), 0)
    return delta


def parse_tc_qdisc_stats(output: str) -> dict[str, int]:
    """Parse the stable counters from `tc -s qdisc show` output."""
    stats = {"sent_bytes": 0, "sent_packets": 0, "dropped": 0}
    for line in output.splitlines():
        words = line.strip().replace("(", "").replace(")", "").split()
        if "rate" in words:
            with suppress(ValueError, IndexError):
                stats["rate_bps"] = int(bandwidth_to_bps(words[words.index("rate") + 1]))
        if "Sent" not in words:
            continue
        with suppress(ValueError, IndexError):
            sent_index = words.index("Sent")
            stats["sent_bytes"] = int(words[sent_index + 1])
            stats["sent_packets"] = int(words[sent_index + 3])
        if "dropped" in words:
            with suppress(ValueError, IndexError):
                stats["dropped"] = int(words[words.index("dropped") + 1].rstrip(","))
    return stats


def bandwidth_to_bps(value: str) -> float:
    """Convert an iproute2-style bandwidth string into bits per second."""
    normalized = value.strip().lower()
    units = {
        "gbit": 1_000_000_000,
        "gbps": 1_000_000_000,
        "mbit": 1_000_000,
        "mbps": 1_000_000,
        "kbit": 1_000,
        "kbps": 1_000,
        "bit": 1,
        "bps": 1,
    }
    for suffix, multiplier in units.items():
        if normalized.endswith(suffix):
            return float(normalized[: -len(suffix)]) * multiplier
    return float(normalized)


def sudo_ip(args: list[str], *, runner: CommandRunner, check: bool = True):
    """Run `ip` with sudo for lab setup operations."""
    return default_debian_backend(runner=runner).sudo_ip(args, check=check)


def sudo_tc(args: list[str], *, runner: CommandRunner, check: bool = True):
    """Run `tc` with sudo for lab shaping operations."""
    return default_debian_backend(runner=runner).sudo_tc(args, check=check)


@dataclass(frozen=True)
class PathHandle:
    """Concrete namespace/interface names for one lab path."""

    client_namespace: str
    server_namespace: str
    client_interface: str
    server_interface: str

    @property
    def namespace_interfaces(self) -> list[tuple[str, str]]:
        """Return both namespace/interface pairs."""
        return [
            (self.client_namespace, self.client_interface),
            (self.server_namespace, self.server_interface),
        ]

    def interfaces_for_side(self, side: LabShapeSide) -> list[tuple[str, str]]:
        """Return the interface pairs affected by one local/remote/both side selector."""
        if side == "local":
            return [(self.client_namespace, self.client_interface)]
        if side == "remote":
            return [(self.server_namespace, self.server_interface)]
        return self.namespace_interfaces


def _ensure_path(config: LabScenarioConfig, path: LabPathConfig, *, runner: CommandRunner) -> PathSetupResult:
    handle = path_handle(config, path)
    client_ns = handle.client_namespace
    server_ns = handle.server_namespace
    client_if = handle.client_interface
    server_if = handle.server_interface

    sudo_ip(["netns", "add", client_ns], runner=runner, check=False)
    sudo_ip(["netns", "add", server_ns], runner=runner, check=False)
    client_link_exists = (
        sudo_ip(["-n", client_ns, "link", "show", client_if], runner=runner, check=False).returncode == 0
    )
    server_link_exists = (
        sudo_ip(["-n", server_ns, "link", "show", server_if], runner=runner, check=False).returncode == 0
    )
    status = "reused" if client_link_exists and server_link_exists else "created"

    if status == "created":
        sudo_ip(["link", "add", client_if, "type", "veth", "peer", "name", server_if], runner=runner, check=False)
        sudo_ip(["link", "set", client_if, "netns", client_ns], runner=runner)
        sudo_ip(["link", "set", server_if, "netns", server_ns], runner=runner)

    prefix = ipaddress.ip_network(path.subnet, strict=False).prefixlen
    sudo_ip(
        ["-n", client_ns, "addr", "add", f"{path.client_address}/{prefix}", "dev", client_if],
        runner=runner,
        check=False,
    )
    sudo_ip(
        ["-n", server_ns, "addr", "add", f"{path.server_address}/{prefix}", "dev", server_if],
        runner=runner,
        check=False,
    )
    sudo_ip(["-n", client_ns, "link", "set", "lo", "up"], runner=runner, check=False)
    sudo_ip(["-n", server_ns, "link", "set", "lo", "up"], runner=runner, check=False)
    sudo_ip(["-n", client_ns, "link", "set", client_if, "up"], runner=runner)
    sudo_ip(["-n", server_ns, "link", "set", server_if, "up"], runner=runner)

    return PathSetupResult(
        name=path.name,
        status=status,
        client_namespace=client_ns,
        server_namespace=server_ns,
        client_interface=client_if,
        server_interface=server_if,
        shape_actions=[],
    )


def has_netem_shape(shape: LabShapeConfig) -> bool:
    """Return whether a shape requires a netem qdisc."""
    return any([shape.rate, shape.delay, shape.jitter, shape.loss, shape.reorder, shape.limit])


def netem_args_for_shape(shape: LabShapeConfig) -> list[str]:
    """Translate lab shape fields into `tc netem` arguments."""
    args: list[str] = []
    if shape.rate:
        args.extend(["rate", shape.rate])
    if shape.delay:
        args.extend(["delay", shape.delay])
        if shape.jitter:
            args.append(shape.jitter)
    if shape.loss:
        args.extend(["loss", shape.loss])
    if shape.reorder:
        args.extend(["reorder", shape.reorder])
    if shape.limit is not None:
        # Keep overload tests honest: a small netem limit turns sustained
        # over-capacity traffic into visible qdisc drops instead of unbounded
        # queuing that eventually arrives as out-of-order traffic.
        args.extend(["limit", str(shape.limit)])
    return args
