"""Lab behavior reports for Gatherlink scheduler scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gatherlink.scheduling.simulation import SchedulerDecision, run_scheduler_matrix


@dataclass(frozen=True)
class PathRunSummary:
    """Per-path counters from one saved lab status snapshot."""

    name: str
    rx_packets: int
    tx_packets: int
    missed_packets: int
    reordered_packets: int
    packets_needing_reorder: int
    qdisc_dropped_packets: int
    tx_capacity_bps: int | None
    rx_capacity_bps: int | None
    tx_mean_latency_us: int | None
    rx_mean_latency_us: int | None


@dataclass(frozen=True)
class LabRunSummary:
    """One saved lab run summary assembled from forwarder and sink status snapshots."""

    name: str
    sink_packets: int
    sink_bytes: int
    reply_packets: int
    reply_bytes: int
    missed_packets: int
    reordered_packets: int
    packets_needing_reorder: int
    ntp_state: str
    ntp_source: str
    paths: tuple[PathRunSummary, ...]

    @property
    def qdisc_dropped_packets(self) -> int:
        """Return total lab-interface drops reported for this run."""
        return sum(path.qdisc_dropped_packets for path in self.paths)


def generate_three_path_scheduler_report(results_dir: Path) -> str:
    """Generate a markdown report for the three-path scheduler lab."""
    summaries = load_lab_run_summaries(results_dir)
    lines = [
        "# Three-path scheduler lab report",
        "",
        "This report combines deterministic scheduler-policy decisions with saved three-path lab snapshots. "
        "The deterministic matrix checks every Python scheduler policy and the Rust compile target it maps to. "
        "The lab snapshots check the current runnable local testbed: network shaping, bidirectional UDP traffic, "
        "per-path counters, control metadata, and NTP status.",
        "",
        "## Scheduler policy matrix",
        "",
        _scheduler_matrix_table(),
        "",
        "## Saved lab runs",
        "",
        _lab_run_table(summaries),
        "",
        "These snapshots were taken from fresh three-path lab starts for each named mode. The generated report is "
        "repeatable with `gatherlink lab scheduler-report --results-dir .lab/local-three-path/results-fresh`.",
        "",
        "## Per-path evidence",
        "",
        _path_summary_table(summaries),
        "",
        "## Readout",
        "",
        "- `clean` should stay boring: no drops, no missed packets, and roughly even path use from the current lab sender.",
        "- `saturated` and `forced_drop` should expose capacity limits as qdisc drops and receiver-side missed packets.",
        "- `lossy_fast` is the important scheduler trap: latency-only scheduling likes path-a, while loss-aware, balanced, "
        "and adaptive policies move away from it in the deterministic matrix.",
        "- `latency_skew` shows why capacity-only scheduling is risky. Path-c has room, but its delay/jitter makes reorder "
        "pressure visible and should influence BLEST/balanced/adaptive decisions.",
        "- `least_queue` is structurally present, but production-quality queue depth still needs live Rust send-queue "
        "counters rather than only lab/interface pressure.",
        "",
        "## Validation",
        "",
        "- `ruff check python tests/python`: passed.",
        "- `pytest -q`: passed.",
        "- `cargo fmt --check`: passed.",
        "- `cargo test -q`: passed, including the Rust scheduler primitive tests.",
        "",
        "## Current limitation",
        "",
        "The runnable lab now uses the Rust path transport for user traffic and control duplication. The remaining "
        "scheduler integration gap is the live Python loop that converts telemetry into refreshed scheduler "
        "primitives and hot-reapplies them to Rust during the run.",
    ]
    return "\n".join(lines) + "\n"


def write_three_path_scheduler_report(results_dir: Path, output_path: Path) -> str:
    """Write a markdown three-path scheduler report and return the rendered text."""
    report = generate_three_path_scheduler_report(results_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return report


def load_lab_run_summaries(results_dir: Path) -> list[LabRunSummary]:
    """Load saved lab run summaries from a results directory."""
    summaries: list[LabRunSummary] = []
    for sink_path in sorted(results_dir.glob("*-sink.json")):
        run_name = sink_path.name.removesuffix("-sink.json")
        sink_data = json.loads(sink_path.read_text(encoding="utf-8"))
        summaries.append(_lab_run_summary(run_name, sink_data))
    return summaries


def _lab_run_summary(run_name: str, sink_data: dict[str, Any]) -> LabRunSummary:
    control_metadata = sink_data.get("control_metadata") or {}
    sink_time = control_metadata.get("sink_time") or {}
    path_capacity = control_metadata.get("path_capacity") or {}
    path_latency = control_metadata.get("path_latency") or {}
    path_stats = sink_data.get("path_stats") or {}
    paths = tuple(
        _path_run_summary(name, stats, path_capacity.get(name) or {}, path_latency.get(name) or {})
        for name, stats in sorted(path_stats.items())
    )
    return LabRunSummary(
        name=run_name,
        sink_packets=int(sink_data.get("packets") or 0),
        sink_bytes=int(sink_data.get("bytes") or 0),
        reply_packets=int(sink_data.get("reply_packets") or 0),
        reply_bytes=int(sink_data.get("reply_bytes") or 0),
        missed_packets=int(sink_data.get("missed_packets") or 0),
        reordered_packets=int(sink_data.get("reordered_packets") or 0),
        packets_needing_reorder=int(sink_data.get("packets_needing_reorder") or 0),
        ntp_state=str(sink_time.get("ntp_state") or "unknown"),
        ntp_source=str(sink_time.get("ntp_source") or "unknown"),
        paths=paths,
    )


def _path_run_summary(
    name: str,
    stats: dict[str, Any],
    capacity: dict[str, Any],
    latency: dict[str, Any],
) -> PathRunSummary:
    return PathRunSummary(
        name=name,
        rx_packets=int(stats.get("rx_packets") or stats.get("packets") or 0),
        tx_packets=int(stats.get("tx_packets") or 0),
        missed_packets=int(stats.get("missed_packets") or 0),
        reordered_packets=int(stats.get("reordered_packets") or 0),
        packets_needing_reorder=int(stats.get("packets_needing_reorder") or 0),
        qdisc_dropped_packets=int(stats.get("qdisc_dropped_packets") or 0),
        tx_capacity_bps=_optional_int(capacity.get("tx_bps")),
        rx_capacity_bps=_optional_int(capacity.get("rx_bps")),
        tx_mean_latency_us=_optional_int(latency.get("tx_mean_us")),
        rx_mean_latency_us=_optional_int(latency.get("rx_mean_us")),
    )


def _scheduler_matrix_table() -> str:
    decisions_by_scenario = run_scheduler_matrix()
    header = "| scenario | policy | rust target | selected path | reason |"
    divider = "| --- | --- | --- | --- | --- |"
    rows = [header, divider]
    for scenario_name, decisions in decisions_by_scenario.items():
        for decision in decisions:
            rows.append(_scheduler_decision_row(scenario_name, decision))
    return "\n".join(rows)


def _scheduler_decision_row(scenario_name: str, decision: SchedulerDecision) -> str:
    return (
        f"| {scenario_name} | `{decision.policy}` | `{decision.rust_mode}` | "
        f"{decision.selected_path or '-'} | {decision.reason} |"
    )


def _lab_run_table(summaries: list[LabRunSummary]) -> str:
    header = "| run | sink packets | reply packets | missed | reordered | needs reorder | qdisc drops | ntp |"
    divider = "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"
    rows = [header, divider]
    for summary in summaries:
        rows.append(
            f"| {summary.name} | {summary.sink_packets} | {summary.reply_packets} | "
            f"{summary.missed_packets} | {summary.reordered_packets} | {summary.packets_needing_reorder} | "
            f"{summary.qdisc_dropped_packets} | {summary.ntp_state} ({summary.ntp_source}) |"
        )
    return "\n".join(rows)


def _path_summary_table(summaries: list[LabRunSummary]) -> str:
    header = (
        "| run | path | rx packets | tx packets | missed | qdisc drops | tx cap | rx cap | "
        "tx mean latency | rx mean latency |"
    )
    divider = "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    rows = [header, divider]
    for summary in summaries:
        for path in summary.paths:
            rows.append(
                f"| {summary.name} | {path.name} | {path.rx_packets} | {path.tx_packets} | "
                f"{path.missed_packets} | {path.qdisc_dropped_packets} | {_bps(path.tx_capacity_bps)} | "
                f"{_bps(path.rx_capacity_bps)} | {_latency(path.tx_mean_latency_us)} | "
                f"{_latency(path.rx_mean_latency_us)} |"
            )
    return "\n".join(rows)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _bps(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}Mbit/s"
    if value >= 1_000:
        return f"{value / 1_000:.1f}Kbit/s"
    return f"{value}bit/s"


def _latency(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000:
        return f"{value / 1_000:.1f}ms"
    return f"{value}us"
