"""
Operator-safe lab bundle generation for repeatable Gatherlink acceptance runs.

Bundles are plans, manifests, and example configs. They are intentionally boring:
Python writes explicit files, explains required commands, and records every
object later cleanup may touch. Privileged OS mutation remains an operator
choice outside bundle generation.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from gatherlink.config.models import GatherlinkConfig
from gatherlink.config.validation import validate_config_file
from gatherlink.shared.models import GatherlinkBaseModel

BundleTopology = Literal["hyperv-three-node"]
PreflightStatus = Literal["pass", "fail", "skipped", "not_configured", "deferred"]


class LabBundleNode(GatherlinkBaseModel):
    """One node described by a generated lab bundle."""

    name: str
    role: Literal["sink", "source", "relay"]
    config_path: str
    monitor_names: list[str] = Field(default_factory=list)
    probe_commands: list[str] = Field(default_factory=list)


class LabBundlePath(GatherlinkBaseModel):
    """One expected private carrier path in a bundle."""

    name: str
    from_node: str
    to_node: str
    source: str
    target: str
    carrier: Literal["udp", "quic-datagram", "http3-datagram"] = "udp"
    expected_capacity_bps: int | None = None


class LabBundleResource(GatherlinkBaseModel):
    """One object that bundle cleanup may inspect or remove."""

    kind: Literal["service", "wireguard-interface", "probe-server", "temporary-file", "diagnostics-jsonl"]
    node: str
    name: str
    command: str | None = None


class LabBundleManifest(GatherlinkBaseModel):
    """Inspectable manifest for a generated v0.9.1 lab bundle."""

    kind: Literal["gatherlink.lab.bundle.manifest"] = "gatherlink.lab.bundle.manifest"
    schema_version: int = 1
    topology: BundleTopology
    nodes: list[LabBundleNode]
    paths: list[LabBundlePath]
    resources: list[LabBundleResource]
    monitor_groups: dict[str, list[str]] = Field(default_factory=dict)
    debian_commands: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LabPreflightFinding(GatherlinkBaseModel):
    """One read-only preflight result from a bundle manifest."""

    code: str
    status: PreflightStatus
    message: str
    node: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class LabPreflightReport(GatherlinkBaseModel):
    """Read-only preflight report for a generated bundle manifest."""

    ok: bool
    manifest: str
    findings: list[LabPreflightFinding]


class LabCleanupPlan(GatherlinkBaseModel):
    """Scoped cleanup plan derived only from manifest resources."""

    manifest: str
    execute: bool = False
    commands: list[str]
    warnings: list[str] = Field(default_factory=list)


class LabCleanupExecutionResult(GatherlinkBaseModel):
    """One guarded manifest cleanup command result."""

    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class GeneratedLabBundle:
    """Paths written during bundle generation."""

    out_dir: Path
    manifest_path: Path
    config_paths: tuple[Path, ...]
    command_path: Path


def generate_lab_bundle(topology: BundleTopology, out_dir: Path) -> GeneratedLabBundle:
    """Generate a supported lab bundle under ``out_dir``."""
    if topology != "hyperv-three-node":
        raise ValueError(f"unsupported lab bundle topology: {topology}")
    return _generate_hyperv_three_node_bundle(out_dir)


def load_lab_bundle_manifest(path: Path) -> LabBundleManifest:
    """Load and validate a generated lab bundle manifest."""
    return LabBundleManifest(**json.loads(path.read_text(encoding="utf-8")))


def preflight_lab_bundle(path: Path) -> LabPreflightReport:
    """Run read-only bundle checks that never contact VMs or mutate the host."""
    manifest = load_lab_bundle_manifest(path)
    findings: list[LabPreflightFinding] = []
    base = path.parent

    for node in manifest.nodes:
        config_path = base / node.config_path
        if not config_path.exists():
            findings.append(
                LabPreflightFinding(
                    code="bundle.config_missing",
                    status="fail",
                    node=node.name,
                    message=f"config file is missing: {node.config_path}",
                )
            )
            continue
        try:
            validate_config_file(config_path)
        except Exception as exc:
            findings.append(
                LabPreflightFinding(
                    code="bundle.config_invalid",
                    status="fail",
                    node=node.name,
                    message=f"config validation failed: {exc}",
                )
            )
        else:
            findings.append(
                LabPreflightFinding(
                    code="bundle.config_valid",
                    status="pass",
                    node=node.name,
                    message=f"config validates: {node.config_path}",
                )
            )

    if manifest.paths:
        findings.append(
            LabPreflightFinding(
                code="bundle.paths_declared",
                status="pass",
                message=f"{len(manifest.paths)} private carrier paths declared",
            )
        )
    else:
        findings.append(
            LabPreflightFinding(
                code="bundle.paths_missing",
                status="fail",
                message="no private carrier paths are declared",
            )
        )

    if manifest.debian_commands:
        findings.append(
            LabPreflightFinding(
                code="bundle.debian_commands",
                status="not_configured",
                message="privileged Debian setup commands are emitted for operator review only",
                details={"commands": manifest.debian_commands},
            )
        )

    if any(resource.kind == "wireguard-interface" for resource in manifest.resources):
        findings.append(
            LabPreflightFinding(
                code="bundle.wireguard_lifecycle",
                status="deferred",
                message="WireGuard interface lifecycle remains explicit operator-owned setup",
            )
        )

    ok = not any(finding.status == "fail" for finding in findings)
    return LabPreflightReport(ok=ok, manifest=str(path), findings=findings)


def plan_lab_bundle_cleanup(path: Path, *, execute: bool = False) -> LabCleanupPlan:
    """Return a cleanup plan scoped only to resources listed in the manifest."""
    manifest = load_lab_bundle_manifest(path)
    commands = []
    for resource in manifest.resources:
        if resource.command:
            commands.append(resource.command)
    warnings = [
        "cleanup uses only manifest-listed resources",
        "review commands before running them with elevated privileges",
    ]
    return LabCleanupPlan(manifest=str(path), execute=execute, commands=commands, warnings=warnings)


def execute_lab_bundle_cleanup(
    path: Path,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[LabCleanupExecutionResult]:
    """
    Execute manifest-scoped cleanup commands with conservative command validation.

    The manifest is the only source of commands. This deliberately supports the
    narrow command shapes generated by Gatherlink lab bundles: closing named
    Gatherlink services and deleting explicit WireGuard lab interfaces. It does
    not execute arbitrary shell text from a modified manifest.
    """
    plan = plan_lab_bundle_cleanup(path, execute=True)
    runner = runner or _run_cleanup_command
    results: list[LabCleanupExecutionResult] = []
    for command in plan.commands:
        argv = _validated_cleanup_argv(command)
        completed = runner(argv)
        results.append(
            LabCleanupExecutionResult(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        )
        if completed.returncode != 0:
            break
    return results


def _run_cleanup_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one already-validated cleanup command without invoking a shell."""
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30)


def _validated_cleanup_argv(command: str) -> list[str]:
    """Return argv for an allowed cleanup command or raise a readable error."""
    argv = shlex.split(command)
    if argv[:3] == ["gatherlink", "services", "close"] and len(argv) == 4:
        return argv
    if argv[:4] == ["sudo", "ip", "link", "del"] and len(argv) == 5 and argv[4].startswith("wg-gl-"):
        return argv
    raise ValueError(f"cleanup command is not allowed by bundle guardrails: {command}")


def is_lab_bundle_manifest(path: Path) -> bool:
    """Return whether ``path`` looks like a lab bundle manifest."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("kind") == "gatherlink.lab.bundle.manifest"


def _generate_hyperv_three_node_bundle(out_dir: Path) -> GeneratedLabBundle:
    out_dir.mkdir(parents=True, exist_ok=True)
    config_dir = out_dir / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)

    configs = {
        "sink-b": _node_config(
            node="sink-b",
            role="server",
            service_target="127.0.0.1:18080",
            service_listen="127.0.0.1:55180",
            paths=[
                ("path-a", "10.91.1.12:57001", "10.91.1.11:56001"),
                ("path-b", "10.91.2.12:57002", "10.91.2.11:56002"),
                ("path-c", "10.91.3.12:57003", "10.91.3.11:56003"),
            ],
        ),
        "source-a": _node_config(
            node="source-a",
            role="client",
            peer="sink-b",
            service_target="127.0.0.1:55180",
            service_listen="127.0.0.1:15180",
            paths=[
                ("path-a", "10.91.1.11:56001", "10.91.1.12:57001"),
                ("path-b", "10.91.2.11:56002", "10.91.2.12:57002"),
                ("path-c", "10.91.3.11:56003", "10.91.3.12:57003"),
            ],
        ),
        "source-c": _node_config(
            node="source-c",
            role="client",
            peer="sink-b",
            service_target="127.0.0.1:55180",
            service_listen="127.0.0.1:25180",
            paths=[
                ("path-a", "10.92.1.13:56001", "10.92.1.12:57001"),
                ("path-b", "10.92.2.13:56002", "10.92.2.12:57002"),
                ("path-c", "10.92.3.13:56003", "10.92.3.12:57003"),
            ],
        ),
    }

    config_paths = []
    for name, config in configs.items():
        path = config_dir / f"{name}.json"
        _write_json(path, config.export_dict())
        config_paths.append(path)

    manifest = LabBundleManifest(
        topology="hyperv-three-node",
        nodes=[
            LabBundleNode(
                name="sink-b",
                role="sink",
                config_path="configs/sink-b.json",
                monitor_names=["vm.shared-sink", "remote:source-a", "remote:source-c"],
                probe_commands=[
                    "curl http://127.0.0.1:18080/",
                    "gatherlink services monitor vm.shared-sink remote:source-a remote:source-c",
                ],
            ),
            LabBundleNode(
                name="source-a",
                role="source",
                config_path="configs/source-a.json",
                monitor_names=["source-a"],
                probe_commands=["curl http://127.0.0.1:15180/"],
            ),
            LabBundleNode(
                name="source-c",
                role="source",
                config_path="configs/source-c.json",
                monitor_names=["source-c"],
                probe_commands=["curl http://127.0.0.1:25180/"],
            ),
        ],
        paths=[
            LabBundlePath(
                name=f"{source}-{path}",
                from_node=source,
                to_node="sink-b",
                source=source_addr,
                target=target_addr,
                expected_capacity_bps=10_000_000,
            )
            for source, tuples in {
                "source-a": [
                    ("path-a", "10.91.1.11:56001", "10.91.1.12:57001"),
                    ("path-b", "10.91.2.11:56002", "10.91.2.12:57002"),
                    ("path-c", "10.91.3.11:56003", "10.91.3.12:57003"),
                ],
                "source-c": [
                    ("path-a", "10.92.1.13:56001", "10.92.1.12:57001"),
                    ("path-b", "10.92.2.13:56002", "10.92.2.12:57002"),
                    ("path-c", "10.92.3.13:56003", "10.92.3.12:57003"),
                ],
            }.items()
            for path, source_addr, target_addr in tuples
        ],
        resources=[
            LabBundleResource(
                kind="service",
                node=node,
                name=name,
                command=f"gatherlink services close {name}",
            )
            for node, name in [
                ("sink-b", "vm.shared-sink"),
                ("source-a", "source-a"),
                ("source-c", "source-c"),
            ]
        ]
        + [
            LabBundleResource(
                kind="wireguard-interface",
                node=node,
                name=interface,
                command=f"sudo ip link del {interface}",
            )
            for node, interface in [
                ("sink-b", "wg-gl-b"),
                ("source-a", "wg-gl-a"),
                ("source-c", "wg-gl-c"),
            ]
        ],
        monitor_groups={
            "wg-demo": ["vm.shared-sink", "remote:source-a", "remote:source-c"],
        },
        debian_commands=[
            "sudo apt-get install wireguard-tools curl",
            "sudo ip link add wg-gl-a type wireguard",
            "sudo ip link add wg-gl-b type wireguard",
            "sudo ip link add wg-gl-c type wireguard",
        ],
        warnings=[
            "bundle generation does not mutate host networking",
            "replace generated session material before using this outside a disposable lab",
        ],
    )
    manifest_path = out_dir / "manifest.json"
    _write_json(manifest_path, manifest.export_dict())

    command_path = out_dir / "commands.md"
    command_path.write_text(_commands_markdown(manifest), encoding="utf-8")
    return GeneratedLabBundle(
        out_dir=out_dir,
        manifest_path=manifest_path,
        config_paths=tuple(config_paths),
        command_path=command_path,
    )


def _node_config(
    *,
    node: str,
    role: str,
    service_target: str,
    service_listen: str,
    paths: list[tuple[str, str, str]],
    peer: str | None = None,
) -> GatherlinkConfig:
    return GatherlinkConfig(
        schema_version=1,
        node=node,
        role=role,  # type: ignore[arg-type]
        peer=peer,
        security={
            "mode": "authenticated",
            "local_receiver_index": _receiver_index(node, "local"),
            "remote_receiver_index": _receiver_index(node, "remote"),
            "send_key": _session_key(node, "send"),
            "receive_key": _session_key(node, "receive"),
        },
        paths=[
            {
                "name": name,
                "interface": name,
                "transport_bind": bind,
                "transport_remote": remote,
                "scheduler": {"tx_capacity_bps": 10_000_000, "rx_capacity_bps": 10_000_000},
            }
            for name, bind, remote in paths
        ],
        services=[
            {
                "name": "wireguard-main",
                "listen": service_listen,
                "target": service_target,
                "return_mode": "peer-scoped-source" if role == "server" else "fixed",
            }
        ],
        helpers={"wireguard": {"enabled": True, "service": "wireguard-main"}},
    )


def _receiver_index(node: str, label: str) -> int:
    digest = sha256(f"gatherlink bundle receiver {node}:{label}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def _session_key(node: str, label: str) -> str:
    return b64encode(sha256(f"gatherlink bundle session {node}:{label}".encode()).digest()).decode()


def _commands_markdown(manifest: LabBundleManifest) -> str:
    lines = [
        "# Gatherlink Lab Bundle Commands",
        "",
        "Review these commands before running them. Bundle generation itself is read-only with respect to host setup.",
        "",
        "## Preflight",
        "",
        "```bash",
        "gatherlink lab preflight manifest.json",
        "```",
        "",
        "## Monitor",
        "",
    ]
    for group, services in manifest.monitor_groups.items():
        lines.extend(["```bash", f"gatherlink services monitor {' '.join(services)}", "```", ""])
        lines.append(f"Group `{group}` watches: {', '.join(services)}")
        lines.append("")
    lines.extend(["## Explicit Debian Setup Commands", ""])
    lines.extend(f"- `{command}`" for command in manifest.debian_commands)
    lines.extend(["", "## Cleanup", "", "```bash", "gatherlink lab cleanup manifest.json", "```", ""])
    return "\n".join(lines)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
