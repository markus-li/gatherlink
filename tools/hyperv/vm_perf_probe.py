#!/usr/bin/env python3
"""
Collect lightweight VM performance counters during Gatherlink benchmarks.

This probe is intentionally read-only and dependency-free. It samples `/proc`
instead of requiring sysstat/perf so the benchmark scripts can run on freshly
prepared Debian VMs without extra packages or privileges.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

HZ = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))


@dataclass(frozen=True)
class CpuSample:
    """One `/proc/stat` CPU sample."""

    total: int
    idle: int


@dataclass(frozen=True)
class ProcessSample:
    """One `/proc/<pid>/stat` process CPU sample."""

    cpu_ticks: int
    rss_pages: int


def parse_args() -> argparse.Namespace:
    """Parse CLI options for one bounded probe run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Substring to match against command lines. Can be passed more than once.",
    )
    parser.add_argument("--netdev", action="append", default=[], help="Network interface to sample.")
    return parser.parse_args()


def read_cpu() -> CpuSample:
    """Read aggregate CPU counters from `/proc/stat`."""
    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return CpuSample(total=sum(values), idle=idle)


def read_udp_snmp() -> dict[str, int]:
    """Read UDP counters from `/proc/net/snmp`."""
    lines = Path("/proc/net/snmp").read_text(encoding="utf-8").splitlines()
    headers: list[str] = []
    values: list[str] = []
    for line in lines:
        if line.startswith("Udp:"):
            parts = line.split()
            if not headers:
                headers = parts[1:]
            else:
                values = parts[1:]
                break
    return {key: int(value) for key, value in zip(headers, values, strict=False)}


def read_netdev(names: list[str]) -> dict[str, dict[str, int]]:
    """Read selected interface counters from `/proc/net/dev`."""
    wanted = set(names)
    output: dict[str, dict[str, int]] = {}
    for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if wanted and name not in wanted:
            continue
        fields = [int(value) for value in rest.split()]
        output[name] = {
            "rx_bytes": fields[0],
            "rx_packets": fields[1],
            "rx_drop": fields[3],
            "tx_bytes": fields[8],
            "tx_packets": fields[9],
            "tx_drop": fields[11],
        }
    return output


def process_cmdline(pid: str) -> str:
    """Read a process command line, returning an empty string for vanished processes."""
    try:
        data = Path("/proc", pid, "cmdline").read_bytes()
    except OSError:
        return ""
    return data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def matching_processes(patterns: list[str]) -> dict[int, str]:
    """Find processes whose command line contains any requested substring."""
    output: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        cmdline = process_cmdline(entry.name)
        if not cmdline:
            continue
        if any(pattern in cmdline for pattern in patterns):
            output[int(entry.name)] = cmdline
    return output


def read_process(pid: int) -> ProcessSample | None:
    """Read process CPU and resident memory counters from `/proc`."""
    try:
        stat = Path("/proc", str(pid), "stat").read_text(encoding="utf-8")
        status = Path("/proc", str(pid), "status").read_text(encoding="utf-8")
    except OSError:
        return None
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    utime = int(fields[11])
    stime = int(fields[12])
    rss_pages = 0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            rss_pages = int(parts[1]) * 1024 // os.sysconf("SC_PAGE_SIZE")
            break
    return ProcessSample(cpu_ticks=utime + stime, rss_pages=rss_pages)


def delta_dict(end: dict[str, int], start: dict[str, int]) -> dict[str, int]:
    """Return signed counter deltas for the union of both counter dictionaries."""
    return {key: end.get(key, 0) - start.get(key, 0) for key in sorted(set(start) | set(end))}


def summarize_processes(
    start: dict[int, ProcessSample],
    end: dict[int, ProcessSample],
    cmdlines: dict[int, str],
    elapsed: float,
) -> list[dict[str, object]]:
    """Summarize per-process CPU and memory deltas."""
    output = []
    for pid in sorted(set(start) | set(end)):
        before = start.get(pid)
        after = end.get(pid)
        if before is None or after is None:
            continue
        cpu_seconds = (after.cpu_ticks - before.cpu_ticks) / HZ
        output.append(
            {
                "pid": pid,
                "cmdline": cmdlines.get(pid, ""),
                "cpu_seconds": cpu_seconds,
                "cpu_percent_one_core": (cpu_seconds / elapsed) * 100 if elapsed > 0 else 0,
                "rss_bytes_end": after.rss_pages * os.sysconf("SC_PAGE_SIZE"),
            }
        )
    return output


def main() -> int:
    """Run the probe and write one JSON report."""
    args = parse_args()
    patterns = args.match or ["gatherlink", "python", "iperf3", "wireguard-go"]
    args.out.parent.mkdir(parents=True, exist_ok=True)

    start_time = time.monotonic()
    start_wall = time.time()
    start_cpu = read_cpu()
    start_udp = read_udp_snmp()
    start_netdev = read_netdev(args.netdev)
    cmdlines = matching_processes(patterns)
    start_processes = {pid: sample for pid in cmdlines if (sample := read_process(pid)) is not None}

    samples = []
    while time.monotonic() - start_time < args.duration:
        time.sleep(args.interval)
        cpu = read_cpu()
        udp = read_udp_snmp()
        samples.append(
            {
                "elapsed_seconds": time.monotonic() - start_time,
                "cpu_total_delta": cpu.total - start_cpu.total,
                "cpu_idle_delta": cpu.idle - start_cpu.idle,
                "udp": delta_dict(udp, start_udp),
            }
        )

    elapsed = time.monotonic() - start_time
    end_cpu = read_cpu()
    end_udp = read_udp_snmp()
    end_netdev = read_netdev(args.netdev)
    end_processes = {pid: sample for pid in start_processes if (sample := read_process(pid)) is not None}
    total_delta = max(end_cpu.total - start_cpu.total, 1)
    idle_delta = max(end_cpu.idle - start_cpu.idle, 0)

    report = {
        "hostname": os.uname().nodename,
        "started_unix": start_wall,
        "elapsed_seconds": elapsed,
        "cpu_busy_percent_all_cores": (1 - (idle_delta / total_delta)) * 100,
        "udp_delta": delta_dict(end_udp, start_udp),
        "netdev_delta": {
            name: delta_dict(end_netdev.get(name, {}), start_netdev.get(name, {}))
            for name in sorted(set(start_netdev) | set(end_netdev))
        },
        "processes": summarize_processes(start_processes, end_processes, cmdlines, elapsed),
        "samples": samples,
    }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
