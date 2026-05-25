"""Run repeatable local three-path WAN profile benchmarks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "configs/lab/local-three-path.json"
DEFAULT_THRESHOLDS = REPO_ROOT / "docs/benchmarks/thresholds.json"
DEFAULT_PROFILES = [
    "acceptance-300-500-700",
    "acceptance-uneven-high",
    "realworld-fiber-plus-5g",
    "realworld-starlink-plus-5g",
    "realworld-starlink-plus-2x5g",
]
DEFAULT_SCHEDULERS = [
    "round_robin",
    "capacity_aware",
    "arrival_guarded_capacity",
    "latency_guarded_capacity",
    "ordered_multipath",
    "ordered_multipath_capacity_aware",
]
DEFAULT_REPORTED_PATH_MTU = 1200
DEFAULT_PAYLOAD_SIZE = 1200
KEEP_RUNTIME_ENV = "GATHERLINK_BENCH_KEEP_RUNTIME"


@dataclass(frozen=True)
class ProfileThreshold:
    """Benchmark threshold facts for one named local WAN profile."""

    name: str
    expected_capacity_mbit: float
    wg_userland_mbit: float
    path_capacity_mbit: tuple[float, float, float]
    pressure_mbit: float
    pass_threshold_delivered_ratio: float
    performance_target_delivered_ratio: float
    path_mtu: int
    payload_size: int
    shape_mtu: int | None


@dataclass(frozen=True)
class BenchResult:
    """One measured local profile run."""

    profile: str
    scheduler: str
    cache_mode: str
    offered_mbit: float
    wg_userland_mbit: float
    path_capacity_mbit: tuple[float, float, float]
    duration_seconds: float
    path_mtu: int
    payload_size: int
    client_tx_mbit: float
    sink_rx_mbit: float
    delivery_ratio: float
    pass_threshold_met: bool
    performance_target_met: bool
    missed_packets: int
    reordered_packets: int
    packets_needing_reorder: int
    path_rx_mbit: dict[str, float]
    path_drops: dict[str, int]

    def export_dict(self) -> dict[str, Any]:
        """Return a stable JSON representation."""
        return {
            "profile": self.profile,
            "scheduler": self.scheduler,
            "cache_mode": self.cache_mode,
            "offered_mbit": self.offered_mbit,
            "wg_userland_mbit": self.wg_userland_mbit,
            "path_capacity_mbit": list(self.path_capacity_mbit),
            "duration_seconds": self.duration_seconds,
            "path_mtu": self.path_mtu,
            "payload_size": self.payload_size,
            "client_tx_mbit": self.client_tx_mbit,
            "sink_rx_mbit": self.sink_rx_mbit,
            "delivery_ratio": self.delivery_ratio,
            "pass_threshold_met": self.pass_threshold_met,
            "performance_target_met": self.performance_target_met,
            "missed_packets": self.missed_packets,
            "reordered_packets": self.reordered_packets,
            "packets_needing_reorder": self.packets_needing_reorder,
            "path_rx_mbit": self.path_rx_mbit,
            "path_drops": self.path_drops,
        }


class NoopRunner:
    """Command runner that lets lab network-mode code seed caches without touching tc."""

    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Return a successful completed process without executing the command."""
        return subprocess.CompletedProcess(command, 0, "", "")


def main(argv: list[str] | None = None) -> int:
    """Run the three-path profile benchmark CLI."""
    args = parse_args(argv)
    thresholds = load_profile_thresholds(args.thresholds)
    profiles = split_csv(args.profiles) if args.profiles else DEFAULT_PROFILES
    schedulers = split_csv(args.schedulers) if args.schedulers else DEFAULT_SCHEDULERS
    cache_modes = split_csv(args.cache_modes)
    validate_requested_values(profiles, schedulers, cache_modes, thresholds)
    out_dir = args.out or REPO_ROOT / ".gatherlink/lab-profile-runs" / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-three-path-profile-bench"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[BenchResult] = []
    for scheduler in schedulers:
        for profile in profiles:
            threshold = thresholds[profile]
            for cache_mode in cache_modes:
                result = run_profile(
                    args.config,
                    out_dir=out_dir,
                    threshold=threshold,
                    scheduler=scheduler,
                    cache_mode=cache_mode,
                    duration_seconds=args.duration,
                    path_mtu=args.path_mtu or threshold.path_mtu,
                    shape_mtu=args.path_mtu or threshold.shape_mtu,
                    payload_size=args.payload_size or threshold.payload_size,
                    dry_run=args.dry_run,
                )
                results.append(result)

    write_reports(out_dir, results)
    print(out_dir)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--profiles", help="Comma-separated network modes. Default is all WAN acceptance profiles.")
    parser.add_argument("--schedulers", help="Comma-separated scheduler modes. Default compares the main four modes.")
    parser.add_argument("--cache-modes", default="cold,warm", help="Comma-separated cold,warm. Default: cold,warm.")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--path-mtu", type=int)
    parser.add_argument(
        "--payload-size",
        type=int,
        help="UDP service payload size. Default comes from the selected profile.",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Render reports without mutating the host.")
    return parser.parse_args(argv)


def split_csv(value: str) -> list[str]:
    """Split a comma-separated CLI value."""
    return [item.strip() for item in value.split(",") if item.strip()]


def validate_requested_values(
    profiles: list[str],
    schedulers: list[str],
    cache_modes: list[str],
    thresholds: dict[str, ProfileThreshold],
) -> None:
    """Reject misspelled benchmark dimensions before mutating lab state."""
    unknown_profiles = sorted(set(profiles) - set(thresholds))
    if unknown_profiles:
        raise ValueError(f"unknown profiles: {', '.join(unknown_profiles)}")
    unknown_cache_modes = sorted(set(cache_modes) - {"cold", "warm"})
    if unknown_cache_modes:
        raise ValueError(f"unknown cache modes: {', '.join(unknown_cache_modes)}")
    if not profiles:
        raise ValueError("at least one profile is required")
    if not schedulers:
        raise ValueError("at least one scheduler is required")
    if not cache_modes:
        raise ValueError("at least one cache mode is required")


def load_profile_thresholds(path: Path) -> dict[str, ProfileThreshold]:
    """Load machine-readable three-path WAN thresholds."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles = raw.get("three_path_wan_profiles")
    if not isinstance(profiles, dict):
        raise ValueError("threshold file missing three_path_wan_profiles")
    return {
        name: ProfileThreshold(
            name=name,
            expected_capacity_mbit=float(data["expected_capacity_mbit"]),
            wg_userland_mbit=float(data.get("wg_userland_mbit", data["expected_capacity_mbit"])),
            path_capacity_mbit=_path_capacity_tuple(data.get("path_capacity_mbit")),
            pressure_mbit=float(data["pressure_mbit"]),
            pass_threshold_delivered_ratio=float(data["pass_threshold_delivered_ratio"]),
            performance_target_delivered_ratio=float(data["performance_target_delivered_ratio"]),
            path_mtu=int(data.get("path_mtu", DEFAULT_REPORTED_PATH_MTU)),
            payload_size=int(data.get("payload_size", DEFAULT_PAYLOAD_SIZE)),
            shape_mtu=int(data["shape_mtu"]) if "shape_mtu" in data else None,
        )
        for name, data in profiles.items()
    }


def run_profile(
    config_path: Path,
    *,
    out_dir: Path,
    threshold: ProfileThreshold,
    scheduler: str,
    cache_mode: str,
    duration_seconds: float,
    path_mtu: int,
    shape_mtu: int | None,
    payload_size: int,
    dry_run: bool,
) -> BenchResult:
    """Run one profile/scheduler/cache combination and return the summary."""
    run_name = f"{threshold.name}-{scheduler}-{cache_mode}"
    run_dir = out_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    scenario_config = write_scenario_config(
        config_path,
        run_dir,
        profile=threshold.name,
        scheduler=scheduler,
        shape_mtu=shape_mtu,
    )
    if dry_run:
        return dry_run_result(
            threshold,
            scheduler=scheduler,
            cache_mode=cache_mode,
            duration_seconds=duration_seconds,
            path_mtu=path_mtu,
            payload_size=payload_size,
        )

    cleanup(scenario_config)
    if cache_mode == "cold":
        remove_capacity_cache(scenario_config)
    seed_profile_capacity(scenario_config, threshold.name)
    try:
        run([".venv/bin/gatherlink", "lab", "up", str(scenario_config)], run_dir / "lab-up.log")
        time.sleep(1.0)
        run(
            [".venv/bin/gatherlink", "lab", "apply-network-mode", str(scenario_config), threshold.name],
            run_dir / "apply-mode.log",
        )
        time.sleep(1.0)
        before_client = service_status("lab.local-three-path")
        before_sink = service_status("lab.local-three-path.sink")
        send = run(
            [
                ".venv/bin/gatherlink",
                "lab",
                "send",
                str(scenario_config),
                "--direction",
                "to-sink",
                "--duration",
                str(duration_seconds),
                "--bandwidth",
                f"{threshold.pressure_mbit:g}mbit",
                "--payload-size",
                str(payload_size),
                "--count",
                "1",
                "--interval",
                "0",
            ],
            run_dir / "send.log",
        )
        (run_dir / "send.stdout").write_text(send.stdout + send.stderr, encoding="utf-8")
        time.sleep(2.0)
        after_client = service_status("lab.local-three-path")
        after_sink = service_status("lab.local-three-path.sink")
    finally:
        cleanup(scenario_config)

    for name, payload in {
        "before-client.json": before_client,
        "before-sink.json": before_sink,
        "after-client.json": after_client,
        "after-sink.json": after_sink,
    }.items():
        (run_dir / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = summarize_run(
        threshold,
        scheduler,
        cache_mode,
        duration_seconds,
        path_mtu,
        payload_size,
        before_client,
        before_sink,
        after_client,
        after_sink,
    )
    (run_dir / "summary.json").write_text(
        json.dumps(result.export_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    prune_runtime_dir(scenario_config)
    return result


def write_scenario_config(
    config_path: Path, run_dir: Path, *, profile: str, scheduler: str, shape_mtu: int | None
) -> Path:
    """Write a temporary scenario config with scheduler and isolated runtime dir."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["scheduler_mode"] = scheduler
    raw["runtime_dir"] = str(run_dir / "runtime")
    _apply_profile_shape_to_paths(raw, profile)
    if shape_mtu is not None:
        for path in raw.get("paths", []):
            shape = path.setdefault("shape", {})
            shape["mtu"] = shape_mtu
    scenario_config = run_dir / "local-three-path.json"
    scenario_config.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return scenario_config


def _apply_profile_shape_to_paths(raw: dict[str, Any], profile: str) -> None:
    """Copy selected profile shape facts onto paths before services start."""
    mode = raw.get("network_modes", {}).get(profile)
    if not isinstance(mode, dict):
        return
    targets = mode.get("targets")
    paths = raw.get("paths")
    if not isinstance(targets, list) or not isinstance(paths, list):
        return
    paths_by_name = {path.get("name"): path for path in paths if isinstance(path, dict)}
    for target in targets:
        if not isinstance(target, dict):
            continue
        path = paths_by_name.get(target.get("path"))
        shape = target.get("shape")
        if path is None or not isinstance(shape, dict):
            continue
        path_shape = path.setdefault("shape", {})
        path_shape.update({key: value for key, value in shape.items() if value is not None})


def seed_profile_capacity(scenario_config: Path, profile: str) -> None:
    """Seed profile rate hints before lab up so the first scheduler state is profile-aware."""
    from gatherlink.lab import apply_lab_network_mode, load_lab_scenario_file

    scenario = load_lab_scenario_file(scenario_config)
    apply_lab_network_mode(scenario, profile, runner=NoopRunner())


def remove_capacity_cache(scenario_config: Path) -> None:
    """Remove runtime capacity cache for a cold-cache run."""
    raw = json.loads(scenario_config.read_text(encoding="utf-8"))
    cache = Path(raw["runtime_dir"]) / "path-capacity-cache.json"
    cache.unlink(missing_ok=True)


def run(command: list[str], log_path: Path) -> subprocess.CompletedProcess[str]:
    """Run one command in the repo and write stdout/stderr to a log."""
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    log_path.write_text(
        f"$ {' '.join(command)}\n\n# stdout\n{completed.stdout}\n# stderr\n{completed.stderr}",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(command)}; see {log_path}")
    return completed


def cleanup(scenario_config: Path) -> None:
    """Best-effort lab cleanup."""
    subprocess.run(
        [".venv/bin/gatherlink", "lab", "cleanup", str(scenario_config)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def prune_runtime_dir(scenario_config: Path) -> None:
    """
    Remove bulky per-service runtime logs from a completed benchmark run.

    The benchmark report keeps command logs, before/after status snapshots, and
    `summary.json`. Raw service logs can grow by many gigabytes in matrix runs,
    so they are opt-in via `GATHERLINK_BENCH_KEEP_RUNTIME=1`.
    """
    if os.environ.get(KEEP_RUNTIME_ENV) == "1":
        return
    raw = json.loads(scenario_config.read_text(encoding="utf-8"))
    runtime_dir = Path(raw["runtime_dir"]).resolve()
    run_dir = scenario_config.parent.resolve()
    try:
        runtime_dir.relative_to(run_dir)
    except ValueError:
        return
    shutil.rmtree(runtime_dir, ignore_errors=True)


def service_status(service: str) -> dict[str, Any]:
    """Return JSON service status."""
    completed = subprocess.run(
        [".venv/bin/gatherlink", "services", "status", service],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def summarize_run(
    threshold: ProfileThreshold,
    scheduler: str,
    cache_mode: str,
    duration_seconds: float,
    path_mtu: int,
    payload_size: int,
    before_client: dict[str, Any],
    before_sink: dict[str, Any],
    after_client: dict[str, Any],
    after_sink: dict[str, Any],
) -> BenchResult:
    """Summarize one run from before/after status snapshots."""
    sink_rx_bytes = total_delta(after_sink, before_sink, "rx_bytes")
    client_tx_bytes = total_delta(after_client, before_client, "tx_bytes")
    sink_rx_mbit = sink_rx_bytes * 8 / duration_seconds / 1_000_000
    client_tx_mbit = client_tx_bytes * 8 / duration_seconds / 1_000_000
    delivery_ratio = sink_rx_mbit / threshold.pressure_mbit if threshold.pressure_mbit else 0.0
    path_rx_mbit = {}
    path_drops = {}
    for path_name in ["path-a", "path-b", "path-c"]:
        path_rx_mbit[path_name] = (
            path_delta(after_sink, before_sink, path_name, "rx_bytes") * 8 / duration_seconds / 1_000_000
        )
        path_drops[path_name] = path_delta(
            after_client, before_client, path_name, "qdisc_dropped_packets"
        ) + path_delta(
            after_sink,
            before_sink,
            path_name,
            "qdisc_dropped_packets",
        )
    return BenchResult(
        profile=threshold.name,
        scheduler=scheduler,
        cache_mode=cache_mode,
        offered_mbit=threshold.pressure_mbit,
        wg_userland_mbit=threshold.wg_userland_mbit,
        path_capacity_mbit=threshold.path_capacity_mbit,
        duration_seconds=duration_seconds,
        path_mtu=path_mtu,
        payload_size=payload_size,
        client_tx_mbit=client_tx_mbit,
        sink_rx_mbit=sink_rx_mbit,
        delivery_ratio=delivery_ratio,
        pass_threshold_met=delivery_ratio >= threshold.pass_threshold_delivered_ratio,
        performance_target_met=delivery_ratio >= threshold.performance_target_delivered_ratio,
        missed_packets=total_delta(after_sink, before_sink, "missed_packets")
        + total_delta(after_client, before_client, "missed_packets"),
        reordered_packets=total_delta(after_sink, before_sink, "reordered_packets")
        + total_delta(after_client, before_client, "reordered_packets"),
        packets_needing_reorder=total_delta(after_sink, before_sink, "packets_needing_reorder")
        + total_delta(after_client, before_client, "packets_needing_reorder"),
        path_rx_mbit=path_rx_mbit,
        path_drops=path_drops,
    )


def total_delta(after: dict[str, Any], before: dict[str, Any], key: str) -> int:
    """Return a top-level integer counter delta."""
    return int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0)


def path_delta(after: dict[str, Any], before: dict[str, Any], path_name: str, key: str) -> int:
    """Return a per-path integer counter delta."""
    return int(after.get("path_stats", {}).get(path_name, {}).get(key, 0) or 0) - int(
        before.get("path_stats", {}).get(path_name, {}).get(key, 0) or 0
    )


def write_reports(out_dir: Path, results: list[BenchResult]) -> None:
    """Write JSON and Markdown benchmark reports."""
    payload = {"generated_at": datetime.now(UTC).isoformat(), "results": [result.export_dict() for result in results]}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown_report(results), encoding="utf-8")


def render_markdown_report(results: list[BenchResult]) -> str:
    """Render a compact Markdown comparison table."""
    lines = [
        "# Three-Path WAN Profile Benchmark",
        "",
    ]
    coordinated_results = [result for result in results if result.scheduler == "coordinated_adaptive"]
    if coordinated_results:
        lines.extend(
            [
                "## Coordinated Adaptive vs Userspace WireGuard",
                "",
                "| path mtu | payload | profile | cache | path cap a/b/c | offered | wg-user | sink rx | delivery | % wg-user | pass | target | path rx a/b/c | drops a/b/c |",
                "| ---: | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
            ]
        )
        for result in coordinated_results:
            path_capacity = "/".join(f"{capacity:g}" for capacity in result.path_capacity_mbit)
            path_rates = "/".join(f"{result.path_rx_mbit[path]:.1f}" for path in ["path-a", "path-b", "path-c"])
            drops = "/".join(str(result.path_drops[path]) for path in ["path-a", "path-b", "path-c"])
            lines.append(
                f"| {result.path_mtu} | {result.payload_size} | `{result.profile}` | `{result.cache_mode}` | "
                f"{path_capacity} | {result.offered_mbit:.0f} | {result.wg_userland_mbit:.0f} | "
                f"{result.sink_rx_mbit:.1f} | {result.delivery_ratio:.1%} | {_wg_userland_ratio(result)} | "
                f"{'yes' if result.pass_threshold_met else 'no'} | "
                f"{'yes' if result.performance_target_met else 'no'} | {path_rates} | {drops} |"
            )
        lines.extend(["", "## Full Scheduler Matrix", ""])
    lines.extend(
        [
            "When `coordinated_adaptive` is present, it appears once per profile/cache/MTU/payload. Other scheduler rows show `% coord`, meaning this row's sink rate divided by the coordinated policy's sink rate for the same group.",
            "",
            "| path mtu | payload | profile | scheduler | cache | path cap a/b/c | offered | wg-user | sink rx | delivery | % wg-user | % coord | pass | target | path rx a/b/c | drops a/b/c |",
            "| ---: | ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
        ]
    )
    coordinated_by_dimension = {
        _comparison_key(result): result for result in results if result.scheduler == "coordinated_adaptive"
    }
    for result in _grouped_with_single_coordinated(results):
        path_capacity = "/".join(f"{capacity:g}" for capacity in result.path_capacity_mbit)
        path_rates = "/".join(f"{result.path_rx_mbit[path]:.1f}" for path in ["path-a", "path-b", "path-c"])
        drops = "/".join(str(result.path_drops[path]) for path in ["path-a", "path-b", "path-c"])
        coord_ratio = _coordinated_ratio(result, coordinated_by_dimension.get(_comparison_key(result)))
        wg_ratio = _wg_userland_ratio(result)
        lines.append(
            f"| {result.path_mtu} | {result.payload_size} | `{result.profile}` | `{result.scheduler}` | "
            f"`{result.cache_mode}` | "
            f"{path_capacity} | {result.offered_mbit:.0f} | {result.wg_userland_mbit:.0f} | "
            f"{result.sink_rx_mbit:.1f} | "
            f"{result.delivery_ratio:.1%} | "
            f"{wg_ratio} | "
            f"{coord_ratio} | "
            f"{'yes' if result.pass_threshold_met else 'no'} | "
            f"{'yes' if result.performance_target_met else 'no'} | {path_rates} | {drops} |"
        )
    lines.append("")
    lines.append(
        "`pass` means the minimum benchmark gate was met. `target` means the current performance target was met."
    )
    lines.append(
        "`% wg-user` is sink throughput divided by the profile's userspace WireGuard baseline; threshold files may override it with `wg_userland_mbit` when a measured value is available."
    )
    lines.append(
        "`% coord` is this row's sink throughput divided by the `coordinated_adaptive` sink throughput for the same profile/cache/MTU/payload group."
    )
    return "\n".join(lines) + "\n"


def _grouped_with_single_coordinated(results: list[BenchResult]) -> list[BenchResult]:
    """Group rows by profile/cache/MTU and show the coordinator once per group."""
    groups: dict[tuple[str, str, int, int], list[BenchResult]] = {}
    key_order: list[tuple[str, str, int, int]] = []
    for result in results:
        key = _comparison_key(result)
        if key not in groups:
            groups[key] = []
            key_order.append(key)
        groups[key].append(result)
    ordered: list[BenchResult] = []
    for key in key_order:
        rows = groups[key]
        coordinated = next((result for result in rows if result.scheduler == "coordinated_adaptive"), None)
        if coordinated is not None:
            ordered.append(coordinated)
        ordered.extend(result for result in rows if result.scheduler != "coordinated_adaptive")
    return ordered


def _coordinated_ratio(result: BenchResult, coordinated: BenchResult | None) -> str:
    """Return the coordinated/adaptive comparison ratio for one row."""
    if result.scheduler == "coordinated_adaptive":
        return "100.0%"
    if coordinated is None or coordinated.sink_rx_mbit <= 0:
        return "-"
    return f"{result.sink_rx_mbit / coordinated.sink_rx_mbit:.1%}"


def _wg_userland_ratio(result: BenchResult) -> str:
    """Return this row's throughput as a percentage of the userspace WG baseline."""
    if result.wg_userland_mbit <= 0:
        return "-"
    return f"{result.sink_rx_mbit / result.wg_userland_mbit:.1%}"


def _comparison_key(result: BenchResult) -> tuple[str, str, int, int]:
    """Return the dimensions where a specialist and coordinator are comparable."""
    return (result.profile, result.cache_mode, result.path_mtu, result.payload_size)


def dry_run_result(
    threshold: ProfileThreshold,
    *,
    scheduler: str,
    cache_mode: str,
    duration_seconds: float,
    path_mtu: int,
    payload_size: int,
) -> BenchResult:
    """Return an empty result for command/report validation."""
    return BenchResult(
        profile=threshold.name,
        scheduler=scheduler,
        cache_mode=cache_mode,
        offered_mbit=threshold.pressure_mbit,
        wg_userland_mbit=threshold.wg_userland_mbit,
        path_capacity_mbit=threshold.path_capacity_mbit,
        duration_seconds=duration_seconds,
        path_mtu=path_mtu,
        payload_size=payload_size,
        client_tx_mbit=0.0,
        sink_rx_mbit=0.0,
        delivery_ratio=0.0,
        pass_threshold_met=False,
        performance_target_met=False,
        missed_packets=0,
        reordered_packets=0,
        packets_needing_reorder=0,
        path_rx_mbit={"path-a": 0.0, "path-b": 0.0, "path-c": 0.0},
        path_drops={"path-a": 0, "path-b": 0, "path-c": 0},
    )


def _path_capacity_tuple(value: Any) -> tuple[float, float, float]:
    """Return configured benchmark path capacities for path-a/b/c."""
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("path_capacity_mbit must contain three values for path-a/b/c")
    return (float(value[0]), float(value[1]), float(value[2]))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
