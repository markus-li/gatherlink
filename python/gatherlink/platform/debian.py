"""
Debian compatibility backend for Gatherlink v0.9.

Python owns platform integration: process management, operator commands, lab
network setup, diagnostics collection, and filesystem layout. Keeping those
calls behind this backend prevents OS-specific details from leaking into helper
or runtime policy code while v0.9 intentionally supports Debian only.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEBIAN_CONFIG_DIR = Path("/etc/gatherlink")
DEBIAN_STATE_DIR = Path("/var/lib/gatherlink")
DEBIAN_RUNTIME_DIR = Path("/run/gatherlink")
DEBIAN_LOG_DIR = Path("/var/log/gatherlink")


class CommandRunner(Protocol):
    """Small command runner interface used by platform adapters and tests."""

    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run one command and return its completed process."""


class SubprocessCommandRunner:
    """Run commands through ``subprocess.run`` with captured text output."""

    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run one command."""
        return subprocess.run(command, check=check, text=True, capture_output=True)

    def run_passthrough(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run one interactive command without capturing terminal output."""
        return subprocess.run(command, check=check, text=True)


@dataclass(frozen=True)
class DebianCompatibilityBackend:
    """Debian-only compatibility operations used by the Python control plane."""

    runner: CommandRunner | None = None

    def command_runner(self) -> CommandRunner:
        """Return the configured command runner or the default subprocess runner."""
        return self.runner or SubprocessCommandRunner()

    def sudo_ip(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run Debian ``ip`` through sudo for lab/setup operations."""
        return self.command_runner().run(["sudo", "ip", *args], check=check)

    def sudo_tc(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run Debian ``tc`` through sudo for lab shaping operations."""
        return self.command_runner().run(["sudo", "tc", *args], check=check)

    def ip(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run unprivileged Debian ``ip`` for read-only namespace/interface queries."""
        return self.command_runner().run(["ip", *args], check=check)

    def tc(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run unprivileged Debian ``tc`` for read-only qdisc queries."""
        return self.command_runner().run(["tc", *args], check=check)

    def namespace_exists(self, namespace: str) -> bool:
        """Return whether a Linux network namespace exists."""
        result = self.ip(["netns", "list", namespace], check=False)
        return result.returncode == 0 and namespace in result.stdout

    def qdisc_stats(self, interface: str) -> subprocess.CompletedProcess[str]:
        """Return ``tc -s qdisc`` output for one visible interface."""
        return self.tc(["-s", "qdisc", "show", "dev", interface], check=False)

    def read_interface_mtu(self, interface: str, *, sys_class_net: Path = Path("/sys/class/net")) -> int | None:
        """Read an interface MTU from Debian sysfs when the interface is visible."""
        try:
            raw = (sys_class_net / interface / "mtu").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def journalctl_command(self, unit: str, *, follow: bool = False, tail: int = 100) -> list[str]:
        """Build the journal command used for systemd-owned service logs."""
        command = ["journalctl", "-u", unit, "--no-pager", "-n", str(tail)]
        if follow:
            command.append("-f")
        return command

    def run_journalctl(self, unit: str, *, follow: bool = False, tail: int = 100) -> subprocess.CompletedProcess[str]:
        """Stream systemd-owned service logs through the operator terminal."""
        runner = self.command_runner()
        command = self.journalctl_command(unit, follow=follow, tail=tail)
        passthrough = getattr(runner, "run_passthrough", None)
        if callable(passthrough):
            return passthrough(command, check=False)
        return runner.run(command, check=False)

    def systemd_is_active(self, unit: str) -> bool:
        """Return whether systemd reports a unit as active on this Debian host."""
        try:
            result = self.command_runner().run(["systemctl", "is-active", "--quiet", unit], check=False)
        except OSError:
            return False
        return result.returncode == 0

    def ntp_synchronization_state(self) -> str:
        """Return the host NTP sync state reported by Debian ``timedatectl``."""
        try:
            result = self.command_runner().run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                check=False,
            )
        except OSError:
            return "unknown"
        value = result.stdout.strip().lower()
        if result.returncode != 0 or value not in {"yes", "no"}:
            return "unknown"
        return "synchronized" if value == "yes" else "unsynchronized"

    @property
    def config_dir(self) -> Path:
        """Return the Debian static config directory for the current release."""
        return DEBIAN_CONFIG_DIR

    @property
    def state_dir(self) -> Path:
        """Return the Debian persistent state/cache directory for the current release."""
        return DEBIAN_STATE_DIR

    @property
    def runtime_dir(self) -> Path:
        """Return the Debian volatile runtime directory for the current release."""
        return DEBIAN_RUNTIME_DIR

    @property
    def log_dir(self) -> Path:
        """Return the Debian local log/event directory for the current release."""
        return DEBIAN_LOG_DIR


def default_debian_backend(*, runner: CommandRunner | None = None) -> DebianCompatibilityBackend:
    """Return the Debian backend used by current-release control-plane code."""
    return DebianCompatibilityBackend(runner=runner)
