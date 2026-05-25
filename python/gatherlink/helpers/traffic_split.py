"""
Local traffic-split helper for advanced dual WireGuard profiles.

This helper owns the operator-facing policy for splitting local traffic between
two WireGuard interfaces. It does not inspect Gatherlink packets, and it does
not change Rust behavior. OS command execution stays behind the Debian platform
backend so the rules remain visible, dry-runnable, and reversible.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Literal

from gatherlink.platform import DebianCompatibilityBackend, default_debian_backend

TrafficSplitMode = Literal["tcp-stable-udp-fast"]


@dataclass(frozen=True)
class TrafficSplitPlan:
    """
    Reversible Debian policy-routing plan for dual WireGuard optimization.

    The default rule sends TCP and non-UDP traffic to the stable tunnel while
    UDP goes to the fast tunnel. Operators can use this as a generated baseline,
    or copy the printed commands into their own firewall management system.
    """

    stable_interface: str
    fast_interface: str
    stable_table: int = 51881
    fast_table: int = 51882
    stable_mark: int = 0x5181
    fast_mark: int = 0x5182
    nft_table: str = "gatherlink_split"
    nft_chain: str = "output"
    rule_comment_prefix: str = "gatherlink dual-wireguard split"
    mode: TrafficSplitMode = "tcp-stable-udp-fast"

    def apply_commands(self) -> list[list[str]]:
        """Return idempotent-ish command steps for applying the split."""
        return [
            ["sudo", "nft", "add", "table", "inet", self.nft_table],
            [
                "sudo",
                "nft",
                "add",
                "chain",
                "inet",
                self.nft_table,
                self.nft_chain,
                "{",
                "type",
                "route",
                "hook",
                "output",
                "priority",
                "mangle",
                ";",
                "}",
            ],
            [
                "sudo",
                "nft",
                "add",
                "rule",
                "inet",
                self.nft_table,
                self.nft_chain,
                "meta",
                "l4proto",
                "udp",
                "meta",
                "mark",
                "set",
                hex(self.fast_mark),
                "comment",
                f"{self.rule_comment_prefix}: udp-fast",
            ],
            [
                "sudo",
                "nft",
                "add",
                "rule",
                "inet",
                self.nft_table,
                self.nft_chain,
                "meta",
                "l4proto",
                "!=",
                "udp",
                "meta",
                "mark",
                "set",
                hex(self.stable_mark),
                "comment",
                f"{self.rule_comment_prefix}: stable-default",
            ],
            [
                "sudo",
                "ip",
                "route",
                "replace",
                "default",
                "dev",
                self.stable_interface,
                "table",
                str(self.stable_table),
            ],
            ["sudo", "ip", "route", "replace", "default", "dev", self.fast_interface, "table", str(self.fast_table)],
            [
                "sudo",
                "ip",
                "-6",
                "route",
                "replace",
                "default",
                "dev",
                self.stable_interface,
                "table",
                str(self.stable_table),
            ],
            [
                "sudo",
                "ip",
                "-6",
                "route",
                "replace",
                "default",
                "dev",
                self.fast_interface,
                "table",
                str(self.fast_table),
            ],
            ["sudo", "ip", "rule", "add", "fwmark", hex(self.stable_mark), "table", str(self.stable_table)],
            ["sudo", "ip", "rule", "add", "fwmark", hex(self.fast_mark), "table", str(self.fast_table)],
        ]

    def revert_commands(self) -> list[list[str]]:
        """Return command steps for removing Gatherlink-managed split state."""
        return [
            ["sudo", "ip", "rule", "del", "fwmark", hex(self.stable_mark), "table", str(self.stable_table)],
            ["sudo", "ip", "rule", "del", "fwmark", hex(self.fast_mark), "table", str(self.fast_table)],
            ["sudo", "ip", "route", "flush", "table", str(self.stable_table)],
            ["sudo", "ip", "route", "flush", "table", str(self.fast_table)],
            ["sudo", "ip", "-6", "route", "flush", "table", str(self.stable_table)],
            ["sudo", "ip", "-6", "route", "flush", "table", str(self.fast_table)],
            ["sudo", "nft", "delete", "table", "inet", self.nft_table],
        ]

    def summary(self) -> dict[str, object]:
        """Return structured facts suitable for diagnostics or `--json` output."""
        return {
            "mode": self.mode,
            "stable_interface": self.stable_interface,
            "fast_interface": self.fast_interface,
            "stable_table": self.stable_table,
            "fast_table": self.fast_table,
            "stable_mark": hex(self.stable_mark),
            "fast_mark": hex(self.fast_mark),
            "nft_table": self.nft_table,
            "nft_chain": self.nft_chain,
            "rule_comment_prefix": self.rule_comment_prefix,
            "warning": (
                "Advanced optimization: review these firewall/routing rules before applying. "
                "Prefer owning the final policy in your normal firewall tooling."
            ),
        }


def render_commands(commands: list[list[str]]) -> str:
    """Render command vectors as shell-copyable lines for operator review."""
    return "\n".join(shlex.join(command) for command in commands)


def execute_traffic_split_commands(
    commands: list[list[str]],
    *,
    backend: DebianCompatibilityBackend | None = None,
    check: bool = True,
) -> list[subprocess.CompletedProcess[str]]:
    """Execute a generated traffic-split command plan through the Debian backend."""
    runner = (backend or default_debian_backend()).command_runner()
    return [runner.run(command, check=check) for command in commands]
