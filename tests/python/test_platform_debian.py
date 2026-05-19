import subprocess
from pathlib import Path

from gatherlink.platform.debian import DebianCompatibilityBackend


class RecordingRunner:
    def __init__(self, *, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode, stdout=self.stdout, stderr="")


def test_debian_backend_wraps_lab_ip_and_tc_commands() -> None:
    runner = RecordingRunner()
    backend = DebianCompatibilityBackend(runner=runner)

    backend.sudo_ip(["netns", "add", "glab-test"])
    backend.sudo_tc(["qdisc", "show"], check=False)

    assert runner.commands == [
        ["sudo", "ip", "netns", "add", "glab-test"],
        ["sudo", "tc", "qdisc", "show"],
    ]


def test_debian_backend_detects_named_namespace_from_ip_output() -> None:
    runner = RecordingRunner(stdout="glab-test (id: 0)\n")
    backend = DebianCompatibilityBackend(runner=runner)

    assert backend.namespace_exists("glab-test") is True
    assert runner.commands == [["ip", "netns", "list", "glab-test"]]


def test_debian_backend_reads_interface_mtu(tmp_path: Path) -> None:
    mtu_path = tmp_path / "eth0"
    mtu_path.mkdir()
    (mtu_path / "mtu").write_text("1420\n", encoding="utf-8")

    backend = DebianCompatibilityBackend()

    assert backend.read_interface_mtu("eth0", sys_class_net=tmp_path) == 1420


def test_debian_backend_builds_journalctl_command() -> None:
    backend = DebianCompatibilityBackend()

    assert backend.journalctl_command("gatherlink-core@test.service", follow=True, tail=25) == [
        "journalctl",
        "-u",
        "gatherlink-core@test.service",
        "--no-pager",
        "-n",
        "25",
        "-f",
    ]


def test_debian_backend_queries_systemd_active_state() -> None:
    runner = RecordingRunner(returncode=0)
    backend = DebianCompatibilityBackend(runner=runner)

    assert backend.systemd_is_active("gatherlink.service") is True
    assert runner.commands == [["systemctl", "is-active", "--quiet", "gatherlink.service"]]


def test_debian_backend_reports_inactive_systemd_unit() -> None:
    runner = RecordingRunner(returncode=3)
    backend = DebianCompatibilityBackend(runner=runner)

    assert backend.systemd_is_active("missing.service") is False
