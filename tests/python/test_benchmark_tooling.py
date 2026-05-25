from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_three_path_bench_module():
    spec = importlib.util.spec_from_file_location(
        "run_three_path_profile_bench",
        REPO_ROOT / "tools/run_three_path_profile_bench.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_three_path_benchmark_thresholds_have_pass_and_target_terms() -> None:
    payload = json.loads((REPO_ROOT / "docs/benchmarks/thresholds.json").read_text(encoding="utf-8"))

    profile = payload["three_path_wan_profiles"]["acceptance-300-500-700"]
    high_profile = payload["three_path_wan_profiles"]["acceptance-uneven-high"]
    wan_profiles = payload["wan_profiles"]
    external_profile = payload["wan_profiles"]["external-five-starlink-correlated"]

    assert profile["pass_threshold_delivered_ratio"] == 0.8
    assert profile["performance_target_delivered_ratio"] == 0.9
    assert profile["path_capacity_mbit"] == [300, 500, 700]
    assert profile["path_mtu"] == 1200
    assert profile["payload_size"] == 1200
    assert high_profile["path_mtu"] == 1452
    assert high_profile["payload_size"] == 1438
    assert high_profile["shape_mtu"] == 1452
    assert {
        "external-clean-dual-gig",
        "external-fiber-5g-asymmetric",
        "external-starlink-5g-high-bdp",
        "external-starlink-queue-dynamics",
        "external-five-starlink-correlated",
        "external-dual-lte-same-tower",
        "external-dual-lte-independent",
        "external-duplication-mode",
        "external-tcp-mode-relay",
    }.issubset(wan_profiles.keys())
    assert wan_profiles["external-clean-dual-gig"]["path_capacity_mbit"] == [1000, 1000]
    assert wan_profiles["external-fiber-5g-asymmetric"]["path_capacity_mbit"] == [800, 150]
    assert wan_profiles["external-starlink-queue-dynamics"]["path_capacity_mbit"] == [160, 95, 20]
    assert wan_profiles["external-starlink-queue-dynamics"]["wg_userland_mbit"] == 232
    assert external_profile["path_capacity_mbit"] == [220, 240, 200, 230, 210]
    assert external_profile["external_pass_mbit"] == 300
    assert external_profile["external_strong_mbit"] == 650


def test_hyperv_five_path_scripts_share_path_validation() -> None:
    common = (REPO_ROOT / "tools/hyperv/perf_common.sh").read_text(encoding="utf-8")
    raw_onehop = (REPO_ROOT / "tools/hyperv/run_gatherlink_onehop_speed.sh").read_text(encoding="utf-8")
    private_lan = (REPO_ROOT / "tools/hyperv/run_private_lan_speed.sh").read_text(encoding="utf-8")
    wireguard = (REPO_ROOT / "tools/hyperv/run_wireguard_onehop_speed.sh").read_text(encoding="utf-8")
    dual_wireguard = (REPO_ROOT / "tools/hyperv/run_dual_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")
    shaper = (REPO_ROOT / "tools/hyperv/apply_path_shape_profile.sh").read_text(encoding="utf-8")
    guest_paths = (REPO_ROOT / "tools/hyperv/configure_guest_path_interfaces.sh").read_text(encoding="utf-8")

    assert 'PERF_USER="${PERF_USER:-gatherlink}"' in common
    assert 'PERF_REMOTE_REPO="${PERF_REMOTE_REPO:-/home/gatherlink/src/gatherlink}"' in common
    assert '"${PERF_USER}@${IP}"' in common
    assert "cd ${PERF_REMOTE_REPO}" in common
    assert 'allowed = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}' in common
    assert "--active-paths must contain one or more of a,b,c,d,e" in common
    assert 'perf_path_indexes "${ACTIVE_PATHS}"' in raw_onehop
    assert 'perf_path_capacity_json "${ACTIVE_PATHS}" "${PATH_CAPACITY_MBITS}"' in raw_onehop
    assert 'perf_path_indexes "${ACTIVE_PATHS}"' in private_lan
    assert 'perf_path_indexes "${ACTIVE_PATHS}"' in wireguard
    assert "kernel|userspace|gotatun|boringtun" in wireguard
    assert "command -v gotatun" in wireguard
    assert "command -v boringtun-cli" in wireguard
    assert "cd ${PERF_REMOTE_REPO}" in dual_wireguard
    assert "private-key ${PERF_REMOTE_HOME}/wg-dual.key" in dual_wireguard
    assert "/home/gatherlink/src/gatherlink" not in dual_wireguard
    assert "external-clean-dual-gig:a)" in shaper
    assert "external-fiber-5g-asymmetric:b)" in shaper
    assert "external-starlink-5g-high-bdp:c)" in shaper
    assert "external-starlink-queue-dynamics:c)" in shaper
    assert "external-five-starlink-correlated:d)" in shaper
    assert "external-five-starlink-correlated:e)" in shaper
    assert "external-dual-lte-same-tower:b)" in shaper
    assert "external-dual-lte-independent:b)" in shaper
    assert "external-duplication-mode:c)" in shaper
    assert "external-tcp-mode-relay:b)" in shaper
    assert 'PERF_USER="${PERF_USER:-gatherlink}"' in shaper
    assert '"${PERF_USER}@${IP}"' in shaper
    assert "Usage: configure_guest_path_interfaces.sh --host-index 11|12|13" in guest_paths
    assert "a) printf '1'" in guest_paths
    assert "e) printf '5'" in guest_paths


def test_three_path_benchmark_dry_run_writes_report(tmp_path: Path) -> None:
    module = _load_three_path_bench_module()

    result = module.main(
        [
            "--profiles",
            "acceptance-300-500-700",
            "--schedulers",
            "capacity_aware",
            "--cache-modes",
            "cold",
            "--duration",
            "1",
            "--path-mtu",
            "9000",
            "--payload-size",
            "8192",
            "--out",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert result == 0
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert "| path mtu | payload | profile | scheduler | cache | path cap a/b/c | offered | wg-user |" in report
    assert (
        "| 9000 | 8192 | `acceptance-300-500-700` | `capacity_aware` | `cold` | 300/500/700 | 1550 | 1500 |" in report
    )
    assert "`acceptance-300-500-700`" in report
    assert "`capacity_aware`" in report
    assert summary["results"][0]["path_mtu"] == 9000
    assert summary["results"][0]["payload_size"] == 8192
    assert summary["results"][0]["path_capacity_mbit"] == [300.0, 500.0, 700.0]
    assert summary["results"][0]["wg_userland_mbit"] == 1500.0
    assert summary["results"][0]["performance_target_met"] is False


def test_three_path_benchmark_groups_one_coordinated_row_with_ratio_comparisons(tmp_path: Path) -> None:
    module = _load_three_path_bench_module()

    result = module.main(
        [
            "--profiles",
            "acceptance-300-500-700",
            "--schedulers",
            "capacity_aware,coordinated_adaptive,flowlet_adaptive",
            "--cache-modes",
            "warm",
            "--duration",
            "1",
            "--out",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert result == 0
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    full_matrix = report.split("## Full Scheduler Matrix", maxsplit=1)[1]
    rows = [line for line in full_matrix.splitlines() if line.startswith("| 1200 |")]
    scheduler_cells = [row.split("|")[4].strip() for row in rows]
    wg_ratio_cells = [row.split("|")[11].strip() for row in rows]
    coord_ratio_cells = [row.split("|")[12].strip() for row in rows]

    assert scheduler_cells == [
        "`coordinated_adaptive`",
        "`capacity_aware`",
        "`flowlet_adaptive`",
    ]
    assert wg_ratio_cells == ["0.0%", "0.0%", "0.0%"]
    assert coord_ratio_cells == ["100.0%", "-", "-"]
    assert "% wg-user" in report
    assert "% coord" in report
    assert "## Coordinated Adaptive vs Userspace WireGuard" in report
    assert "| 1200 | 1200 | `acceptance-300-500-700` | `warm` | 300/500/700 | 1550 | 1500 |" in report


def test_three_path_benchmark_uses_profile_mtu_and_payload_defaults(tmp_path: Path) -> None:
    module = _load_three_path_bench_module()

    result = module.main(
        [
            "--profiles",
            "acceptance-uneven-high",
            "--schedulers",
            "capacity_aware",
            "--cache-modes",
            "cold",
            "--duration",
            "1",
            "--out",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert result == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    scenario = json.loads(
        (tmp_path / "acceptance-uneven-high-capacity_aware-cold" / "local-three-path.json").read_text(encoding="utf-8")
    )
    assert summary["results"][0]["path_mtu"] == 1452
    assert summary["results"][0]["payload_size"] == 1438
    assert all(path["shape"]["mtu"] == 1452 for path in scenario["paths"])


def test_three_path_benchmark_does_not_force_shape_mtu_without_profile_request(tmp_path: Path) -> None:
    module = _load_three_path_bench_module()

    result = module.main(
        [
            "--profiles",
            "realworld-starlink-plus-2x5g",
            "--schedulers",
            "capacity_aware",
            "--cache-modes",
            "cold",
            "--duration",
            "1",
            "--out",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert result == 0
    scenario = json.loads(
        (tmp_path / "realworld-starlink-plus-2x5g-capacity_aware-cold" / "local-three-path.json").read_text(
            encoding="utf-8"
        )
    )
    assert all("mtu" not in path.get("shape", {}) for path in scenario["paths"])


def test_three_path_benchmark_copies_profile_shape_to_runtime_paths(tmp_path: Path) -> None:
    module = _load_three_path_bench_module()

    result = module.main(
        [
            "--profiles",
            "realworld-fiber-plus-5g",
            "--schedulers",
            "latency_guarded_capacity",
            "--cache-modes",
            "cold",
            "--duration",
            "1",
            "--out",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert result == 0
    scenario = json.loads(
        (tmp_path / "realworld-fiber-plus-5g-latency_guarded_capacity-cold" / "local-three-path.json").read_text(
            encoding="utf-8"
        )
    )
    assert [path["shape"]["delay"] for path in scenario["paths"]] == ["12ms", "45ms", "70ms"]
    assert [path["shape"]["jitter"] for path in scenario["paths"]] == ["3ms", "15ms", "25ms"]


def test_three_path_benchmark_prunes_runtime_dir_by_default(tmp_path: Path, monkeypatch) -> None:
    module = _load_three_path_bench_module()
    run_dir = tmp_path / "run"
    runtime_dir = run_dir / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "service.log").write_text("large generated log", encoding="utf-8")
    scenario = run_dir / "local-three-path.json"
    scenario.write_text(json.dumps({"runtime_dir": str(runtime_dir)}) + "\n", encoding="utf-8")

    monkeypatch.delenv("GATHERLINK_BENCH_KEEP_RUNTIME", raising=False)
    module.prune_runtime_dir(scenario)

    assert not runtime_dir.exists()


def test_three_path_benchmark_can_keep_runtime_dir_for_debugging(tmp_path: Path, monkeypatch) -> None:
    module = _load_three_path_bench_module()
    run_dir = tmp_path / "run"
    runtime_dir = run_dir / "runtime"
    runtime_dir.mkdir(parents=True)
    scenario = run_dir / "local-three-path.json"
    scenario.write_text(json.dumps({"runtime_dir": str(runtime_dir)}) + "\n", encoding="utf-8")

    monkeypatch.setenv("GATHERLINK_BENCH_KEEP_RUNTIME", "1")
    module.prune_runtime_dir(scenario)

    assert runtime_dir.exists()


def test_onehop_wireguard_probe_duration_tracks_benchmark_window() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_onehop_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert "benchmark_sections=$((RUN_TCP + RUN_UDP))" in script
    assert 'if [[ "${RUN_MIXED}" -eq 1 ]]; then' in script
    assert "benchmark_sections=1" in script
    assert "probe_duration=$((DURATION * benchmark_sections + 2))" in script
    assert "probe_duration=$((DURATION * benchmark_sections + 12))" not in script


def test_onehop_wireguard_keeps_setup_services_alive() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_onehop_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")
    setup_call = script.split('"${SCRIPT_DIR}/run_gatherlink_onehop_speed.sh"', maxsplit=1)[1].split(
        "| tee", maxsplit=1
    )[0]

    assert "--setup-only" in setup_call
    assert "--keep-running" in setup_call


def test_onehop_raw_setup_only_does_not_install_exit_cleanup_trap() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_gatherlink_onehop_speed.sh").read_text(encoding="utf-8")

    assert 'if [[ "${KEEP_RUNNING}" -eq 0 && "${SETUP_ONLY}" -eq 0 ]]; then' in script
    assert 'pkill -f "gatherlink.cli.main run service /tmp/gl-onehop-node"' in script


def test_onehop_raw_has_explicit_cleanup_only_mode() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_gatherlink_onehop_speed.sh").read_text(encoding="utf-8")

    assert "CLEANUP_ONLY=0" in script
    assert "--cleanup-only        Stop generated one-hop services" in script
    assert "--cleanup-only) CLEANUP_ONLY=1; shift ;;" in script
    assert 'if [[ "${CLEANUP_ONLY}" -eq 1 ]]; then' in script
    assert "Gatherlink raw UDP one-hop cleanup complete." in script


def test_background_iperf_fetch_waits_for_json_flush() -> None:
    script = (REPO_ROOT / "tools/hyperv/perf_common.sh").read_text(encoding="utf-8")

    assert "perf_wait_remote_file()" in script
    assert 'perf_wait_remote_file "${client_port}" "/tmp/${label}.json" 30' in script
    assert "High-BDP mixed WireGuard tests can finish iperf traffic" in script


def test_dual_wireguard_runner_supports_concurrent_mixed_mode() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_dual_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert "RUN_MIXED=0" in script
    assert "--mixed              Run stable TCP and fast UDP concurrently." in script
    assert "--mixed) RUN_MIXED=1; shift ;;" in script
    assert 'if [[ "${RUN_MIXED}" -eq 1 ]]; then' in script
    assert "dual-wg-stable-mixed-tcp" in script
    assert "dual-wg-fast-mixed-udp" in script


def test_dual_wireguard_runner_exposes_scheduler_traffic_bias() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_dual_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert 'SCHEDULER_TRAFFIC_BIAS="udp"' in script
    assert "--scheduler-traffic-bias BIAS" in script
    assert '--scheduler-traffic-bias) SCHEDULER_TRAFFIC_BIAS="$2"; shift 2 ;;' in script
    assert "'traffic_bias': '${SCHEDULER_TRAFFIC_BIAS}'" in script
    assert "- scheduler_traffic_bias: ${SCHEDULER_TRAFFIC_BIAS}" in script


def test_dual_wireguard_runner_enables_scheduler_reapply_by_default() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_dual_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert "SCHEDULER_REAPPLY_INTERVAL=1" in script
    assert "--scheduler-reapply-interval SECONDS" in script
    assert '--scheduler-reapply-interval) SCHEDULER_REAPPLY_INTERVAL="$2"; shift 2 ;;' in script
    assert "- scheduler_reapply_interval: ${SCHEDULER_REAPPLY_INTERVAL}" in script
    assert "--scheduler-reapply-interval ${SCHEDULER_REAPPLY_INTERVAL}" in script


def test_dual_wireguard_runner_can_apply_explicit_path_pacing() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_dual_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert 'PATH_PACING_MBITS=""' in script
    assert "--path-pacing-mbits SPEC" in script
    assert '--path-pacing-mbits) PATH_PACING_MBITS="$2"; shift 2 ;;' in script
    assert "PATH_PACING_JSON=" in script
    assert "- path_pacing_mbits: ${PATH_PACING_MBITS:-[none]}" in script
    assert "pacing_bps_by_path = ${PATH_PACING_JSON}" in script
    assert "scheduler['pacing_budget_bps'] = pacing_bps_by_path[path_letter]" in script


def test_dual_wireguard_runner_uses_per_service_path_policies() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_dual_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert 'STABLE_PATH_POLICY="single_best_path"' in script
    assert 'FAST_PATH_POLICY="weighted_round_robin"' in script
    assert "STABLE_POLL_BATCH_PACKETS=128" in script
    assert "--stable-paths LIST" in script
    assert "--fast-paths LIST" in script
    assert "--fast-path-headroom MULTIPLIER" in script
    assert "--stable-path-policy POLICY" in script
    assert "--fast-path-policy POLICY" in script
    assert "--stable-poll-batch-packets N" in script
    assert '--stable-poll-batch-packets) STABLE_POLL_BATCH_PACKETS="$2"; shift 2 ;;' in script
    assert "best = max(active_paths" in script
    assert 'FAST_PATH_HEADROOM="1.25"' in script
    assert "required_mbit = target_mbit * headroom" in script
    assert "selected_capacity >= required_mbit" in script
    assert "smallest path set that satisfies the configured" in script
    assert "'scheduler_path_policy': '${STABLE_PATH_POLICY}'" in script
    assert "'scheduler_path_policy': '${FAST_PATH_POLICY}'" in script
    assert "'priority': 'high'" in script
    assert "'scheduler_poll_batch_packets': ${STABLE_POLL_BATCH_PACKETS}" in script
    assert "'priority': 'bulk'" in script
    assert "'scheduler_poll_batch_packets': ${FAST_POLL_BATCH_PACKETS}" in script
    assert "'scheduler_allowed_paths': stable_allowed_paths" in script
    assert "'scheduler_allowed_paths': fast_allowed_paths" in script
    assert "'scheduler_path_weights': stable_path_weights" in script
    assert "'scheduler_path_weights': fast_path_weights" in script
    assert "- service_scheduler_path_weights: capacity-derived per service" in script


def test_hyperv_iperf_tcp_extra_args_are_reported_and_used() -> None:
    common = (REPO_ROOT / "tools/hyperv/perf_common.sh").read_text(encoding="utf-8")
    runner = (REPO_ROOT / "tools/hyperv/run_onehop_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")
    dual_runner = (REPO_ROOT / "tools/hyperv/run_dual_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert 'PERF_IPERF_TCP_CLIENT_ARGS="${PERF_IPERF_TCP_CLIENT_ARGS:-}"' in common
    assert 'PERF_IPERF_TCP_SERVER_ARGS="${PERF_IPERF_TCP_SERVER_ARGS:-}"' in common
    assert "${PERF_IPERF_TCP_CLIENT_ARGS} --json" in common
    assert "${PERF_IPERF_TCP_SERVER_ARGS} --logfile" in common
    assert "- iperf_tcp_client_args: ${PERF_IPERF_TCP_CLIENT_ARGS:-[none]}" in runner
    assert "- iperf_tcp_server_args: ${PERF_IPERF_TCP_SERVER_ARGS:-[none]}" in runner
    assert "- iperf_tcp_client_args: ${PERF_IPERF_TCP_CLIENT_ARGS:-[none]}" in dual_runner
    assert "- iperf_tcp_server_args: ${PERF_IPERF_TCP_SERVER_ARGS:-[none]}" in dual_runner


def test_onehop_speed_runners_can_enable_live_scheduler_reapply() -> None:
    raw_runner = (REPO_ROOT / "tools/hyperv/run_gatherlink_onehop_speed.sh").read_text(encoding="utf-8")
    wg_runner = (REPO_ROOT / "tools/hyperv/run_onehop_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert 'SCHEDULER_REAPPLY_INTERVAL=""' in raw_runner
    assert "--scheduler-reapply-interval SECONDS" in raw_runner
    assert '--scheduler-reapply-interval) SCHEDULER_REAPPLY_INTERVAL="$2"; shift 2 ;;' in raw_runner
    assert "--scheduler-reapply-interval must be greater than zero" in raw_runner
    assert 'scheduler_reapply_arg=" --scheduler-reapply-interval ${SCHEDULER_REAPPLY_INTERVAL}"' in raw_runner
    assert "- scheduler_reapply_interval: ${SCHEDULER_REAPPLY_INTERVAL:-disabled}" in raw_runner

    assert 'SCHEDULER_REAPPLY_INTERVAL=""' in wg_runner
    assert "--scheduler-reapply-interval SECONDS" in wg_runner
    assert '--scheduler-reapply-interval) SCHEDULER_REAPPLY_INTERVAL="$2"; shift 2 ;;' in wg_runner
    assert 'scheduler_reapply_arg=(--scheduler-reapply-interval "${SCHEDULER_REAPPLY_INTERVAL}")' in wg_runner
    assert '"${scheduler_reapply_arg[@]}"' in wg_runner
    assert "- scheduler_reapply_interval: ${SCHEDULER_REAPPLY_INTERVAL:-disabled}" in wg_runner


def test_dual_wireguard_runner_reuses_named_shape_profiles() -> None:
    script = (REPO_ROOT / "tools/hyperv/run_dual_wireguard_gatherlink_speed.sh").read_text(encoding="utf-8")

    assert 'SHAPE_PROFILE="clean"' in script
    assert "--shape-profile NAME" in script
    assert '--shape-profile) SHAPE_PROFILE="$2"; shift 2 ;;' in script
    assert '"${SCRIPT_DIR}/apply_path_shape_profile.sh"' in script
    assert '--profile "${SHAPE_PROFILE}"' in script
