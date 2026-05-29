from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path

from gatherlink.lab.acceptance import AcceptanceCheck, AcceptanceReport

REPO_ROOT = Path(__file__).resolve().parents[2]
VM_TOOLS = REPO_ROOT / "tools" / "vm_acceptance"
HYPERV_TOOLS = REPO_ROOT / "tools" / "hyperv"


def test_vm_acceptance_scripts_are_syntax_valid(tmp_path) -> None:
    script = VM_TOOLS / "run_acceptance.sh"
    validator = VM_TOOLS / "validate_jsonl.py"
    report_writer = VM_TOOLS / "write_report_json.py"

    subprocess.run(["bash", "-n", str(script)], check=True)
    for source in (validator, report_writer):
        subprocess.run(
            [
                "python3",
                "-c",
                ("import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)"),
                str(source),
                str(tmp_path / f"{source.name}.pyc"),
            ],
            check=True,
        )


def test_acceptance_report_schema_marks_failed_checks_not_ok() -> None:
    report = AcceptanceReport(
        mode="dry-run",
        inventory="inventory.example.env",
        output=".gatherlink/example",
        checks=[
            AcceptanceCheck(code="example.pass", status="pass", message="good"),
            AcceptanceCheck(code="example.deferred", status="deferred", message="operator-owned"),
        ],
    )
    assert report.ok is True

    report.checks.append(AcceptanceCheck(code="example.fail", status="fail", message="bad"))
    assert report.ok is False


def test_hyperv_acceptance_scripts_are_syntax_valid() -> None:
    """Keep the VM acceptance shell entrypoints parseable before real VM runs."""
    for script in HYPERV_TOOLS.glob("run_*_acceptance.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_hyperv_performance_scripts_are_syntax_valid() -> None:
    """Performance tooling should stay parseable because it drives long VM runs."""
    for name in [
        "perf_common.sh",
        "install_boringtun_backend.sh",
        "install_gotatun_backend.sh",
        "run_private_lan_speed.sh",
        "run_wireguard_onehop_speed.sh",
        "run_gatherlink_onehop_speed.sh",
        "run_relay_udp_speed.sh",
        "run_relay_rust_udp_speed.sh",
        "run_relay_wireguard_speed.sh",
        "run_performance_matrix.sh",
        "run_direct_wireguard_routing_speed.sh",
    ]:
        subprocess.run(["bash", "-n", str(HYPERV_TOOLS / name)], check=True)


def test_hyperv_performance_docs_define_comparable_layers() -> None:
    """Benchmark notes should keep Gatherlink/WireGuard concerns separated."""
    readme = (REPO_ROOT / "docs" / "benchmarks" / "README.md").read_text(encoding="utf-8")
    log = (REPO_ROOT / "docs" / "benchmarks" / "hyperv-performance-log.md").read_text(encoding="utf-8")

    assert "Private LAN" in log
    assert "Direct WireGuard" in log
    assert "One-hop WireGuard userspace" in log
    assert "wireguard-gotatun-onehop" in log
    assert "wireguard-boringtun-onehop" in log
    assert "Gatherlink raw one-hop" in log
    assert "Gatherlink raw relay" in log
    assert "WireGuard over Gatherlink relay" in log
    assert "Only compare runs with the same" in readme
    assert "install_gotatun_backend.sh" in readme
    assert "install_boringtun_backend.sh" in readme
    assert "CPU and memory are not ruled out" in log


def test_socks5_acceptance_uses_distinct_gatherlink_services_for_helper_types() -> None:
    """SOCKS5 and TCP forward probes must not share one learned app source."""
    script = (HYPERV_TOOLS / "run_socks5_vm_acceptance.sh").read_text(encoding="utf-8")

    assert "'stream-socks5'" in script
    assert "'stream-tcp-forward'" in script
    assert "--gatherlink-service 127.0.0.1:55180" in script
    assert "--gatherlink-service 127.0.0.1:55181" in script


def test_relay_wireguard_acceptance_uses_real_wg_and_relay_processes() -> None:
    """The B -> C -> A proof should exercise production relay/core services."""
    script = (HYPERV_TOOLS / "run_relay_wireguard_vm_acceptance.sh").read_text(encoding="utf-8")

    assert "sudo ip link add wg-gl-a type wireguard" in script
    assert "sudo ip link add wg-gl-b type wireguard" in script
    assert "run relay-start" in script
    assert "relaywg.c.relay.ba.path-" in script
    assert "relaywg.a.exit.ba.path-" not in script
    assert "10.91.{index}.11:{61100 + index}" in script
    assert "'helpers': {'wireguard': {'enabled': True, 'service': 'wireguard-main'}}" in script
    assert "curl --interface wg-gl-b" in script
    assert "--allow-non-loopback" in script
    assert "--view graph --once" in script
    assert "route_id" not in script


def test_hyperv_vm_ip_cache_uses_unique_temporary_files() -> None:
    """Parallel cache writers should not fight over one shared .tmp path."""
    helper = (HYPERV_TOOLS / "vm_ip_cache.sh").read_text(encoding="utf-8")

    assert 'mktemp "${cache_file}.XXXXXX"' in helper
    assert '"${cache_file}.tmp"' not in helper


def test_performance_tooling_collects_live_vm_counters() -> None:
    """Speed labs should capture live CPU and UDP counters during pressure windows."""
    probe = (HYPERV_TOOLS / "vm_perf_probe.py").read_text(encoding="utf-8")
    relay_udp = (HYPERV_TOOLS / "run_relay_udp_speed.sh").read_text(encoding="utf-8")
    relay_rust = (HYPERV_TOOLS / "run_relay_rust_udp_speed.sh").read_text(encoding="utf-8")
    relay_wg = (HYPERV_TOOLS / "run_relay_wireguard_speed.sh").read_text(encoding="utf-8")

    assert "/proc/net/snmp" in probe
    assert "cpu_busy_percent_all_cores" in probe
    assert "udp_delta" in probe
    assert "vm_perf_probe.py" in relay_udp
    assert "--scheduler-mode" in relay_udp
    assert "--path-capacity-mbit" in relay_udp
    assert "reorder_hold not in ('', 'auto')" in relay_udp
    assert "rustc --edition 2021 -O tools/udp_pressure.rs" in relay_rust
    assert "--scheduler-mode" in relay_rust
    assert "--path-capacity-mbit" in relay_rust
    assert "gatherlink-udp-pressure send --target 127.0.0.1:55180" in relay_rust
    assert "gatherlink-udp-pressure sink --bind 127.0.0.1:19091" in relay_rust
    assert "perf_start_node_probe" in relay_wg


def test_udp_pressure_tool_compiles(tmp_path) -> None:
    """The compiled UDP pressure tool is the fair raw-UDP Gatherlink generator."""
    source = REPO_ROOT / "tools" / "udp_pressure.rs"
    output = tmp_path / "udp-pressure"

    subprocess.run(["rustc", "--edition", "2021", "-O", str(source), "-o", str(output)], check=True)

    help_text = subprocess.run(
        [str(output)],
        check=False,
        text=True,
        capture_output=True,
    ).stderr
    assert "--flows N" in help_text
    assert "--workers N" in help_text
    assert "--send-batch N" in help_text
    assert "--recv-batch N" in help_text
    assert "--recv-buffer-size BYTES" in help_text
    assert "--recv-truncate" in help_text
    assert "--udp-gso-segments N" in help_text
    assert "--feedback-bind ADDR" in help_text
    assert "--feedback-initial-mbit MBIT" in help_text
    assert "--feedback-max-mbit MBIT" in help_text
    assert "--feedback-probe-step-mbit MBIT" in help_text
    assert "--feedback-good-ratio RATIO" in help_text
    assert "--feedback-low-ratio RATIO" in help_text
    assert "--feedback-backoff-ratio RATIO" in help_text
    assert "--feedback-target ADDR" in help_text


def test_udp_pressure_multisocket_sink_reports_active_receive_window(tmp_path) -> None:
    """Idle worker sockets must not dilute active sink throughput."""
    source = REPO_ROOT / "tools" / "udp_pressure.rs"
    output = tmp_path / "udp-pressure"
    subprocess.run(["rustc", "--edition", "2021", "-O", str(source), "-o", str(output)], check=True)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        base_port = probe.getsockname()[1]

    sink = subprocess.Popen(
        [
            str(output),
            "sink",
            "--bind",
            f"127.0.0.1:{base_port}",
            "--duration",
            "3",
            "--idle-after-first",
            "0.2",
            "--workers",
            "2",
            "--bind-port-stride",
            "16",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.2)
    subprocess.run(
        [
            str(output),
            "send",
            "--target",
            f"127.0.0.1:{base_port}",
            "--duration",
            "1",
            "--payload-size",
            "1200",
            "--target-mbit",
            "20",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    stdout, stderr = sink.communicate(timeout=5)

    assert sink.returncode == 0, stderr
    stats = json.loads(stdout)
    assert stats["packets"] > 0
    assert stats["elapsed_seconds"] < 1.8
    assert stats["bits_per_second"] > 15_000_000


def test_vm_acceptance_dry_run_does_not_contact_vms(tmp_path) -> None:
    output = tmp_path / "vm-report"
    script = VM_TOOLS / "run_acceptance.sh"

    result = subprocess.run(
        [str(script), "--dry-run", "--out", str(output)],
        check=True,
        text=True,
        capture_output=True,
    )

    commands = (output / "commands.log").read_text(encoding="utf-8")
    report = (output / "report.md").read_text(encoding="utf-8")
    assert "VM acceptance dry-run complete" in result.stdout
    assert "valid:" in result.stdout
    assert "ssh debian-vm-a" in commands
    assert "ssh debian-vm-b" in commands
    assert "[validate-node-a]" in commands
    assert "[monitor-node-a]" in commands
    assert "[diagnostics-node-a]" in commands
    assert "validate_jsonl.py" in commands
    assert "mode: dry-run" in report
    assert "configs validated locally" in report
    assert "diagnostics JSONL are checked" in report
    report_json = AcceptanceReport.model_validate_json((output / "report.json").read_text(encoding="utf-8"))
    assert report_json.mode == "dry-run"
    assert any(check.code == "vm.config.validated" and check.status == "pass" for check in report_json.checks)
    assert any(check.code == "vm.remote.prepare" and check.status == "skipped" for check in report_json.checks)
    assert (output / "node-a.json").exists()
    assert (output / "node-b.json").exists()


def test_vm_acceptance_execute_refuses_committed_example_keys(tmp_path) -> None:
    output = tmp_path / "vm-report"
    script = VM_TOOLS / "run_acceptance.sh"

    result = subprocess.run(
        [str(script), "--execute", "--out", str(output)],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "refusing --execute with example or placeholder authenticated session key" in result.stderr
    assert not (output / "commands.log").exists()


def test_vm_acceptance_committed_files_do_not_contain_private_lab_hosts() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in VM_TOOLS.rglob("*")
        if path.is_file() and path.name != "README.md" and "__pycache__" not in path.parts
    )

    assert ("personal-user" + "@") not in combined
    assert ("10.10." + "18.51") not in combined
