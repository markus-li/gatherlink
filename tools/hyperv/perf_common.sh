#!/usr/bin/env bash
# Shared helpers for repeatable Hyper-V performance probes.
#
# These helpers intentionally stay small and boring. Scenario scripts own the
# topology they build; this file owns only common SSH, reporting, cleanup, and
# iperf result extraction so Gatherlink/WireGuard comparisons use one format.
set -euo pipefail

perf_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "${script_dir}/../.." && pwd
}

perf_init_defaults() {
  REPO_ROOT="${REPO_ROOT:-$(perf_repo_root)}"
  IP="${IP:-172.22.0.1}"
  PORT_A="${PORT_A:-2201}"
  PORT_B="${PORT_B:-2202}"
  PORT_C="${PORT_C:-2203}"
  DURATION="${DURATION:-20}"
  PARALLEL="${PARALLEL:-6}"
  UDP_RATE="${UDP_RATE:-1000M}"
  UDP_LENGTH="${UDP_LENGTH:-1200}"
  PERF_UDP_PRESSURE_FLOWS="${PERF_UDP_PRESSURE_FLOWS:-1}"
  PERF_UDP_PRESSURE_WORKERS="${PERF_UDP_PRESSURE_WORKERS:-1}"
  PERF_UDP_PRESSURE_PORT_STRIDE="${PERF_UDP_PRESSURE_PORT_STRIDE:-16}"
  PERF_UDP_PRESSURE_SEND_BATCH="${PERF_UDP_PRESSURE_SEND_BATCH:-64}"
  PERF_UDP_PRESSURE_RECV_BATCH="${PERF_UDP_PRESSURE_RECV_BATCH:-128}"
  PERF_UDP_PRESSURE_RECV_BUFFER_SIZE="${PERF_UDP_PRESSURE_RECV_BUFFER_SIZE:-65535}"
  PERF_UDP_PRESSURE_RECV_TRUNCATE="${PERF_UDP_PRESSURE_RECV_TRUNCATE:-0}"
  PERF_UDP_PRESSURE_SINK_CPUSET="${PERF_UDP_PRESSURE_SINK_CPUSET:-}"
  PERF_UDP_PRESSURE_SEND_CPUSET="${PERF_UDP_PRESSURE_SEND_CPUSET:-}"
  PERF_UDP_PRESSURE_GSO_SEGMENTS="${PERF_UDP_PRESSURE_GSO_SEGMENTS:-1}"
  PERF_UDP_PRESSURE_FEEDBACK="${PERF_UDP_PRESSURE_FEEDBACK:-0}"
  PERF_UDP_PRESSURE_FEEDBACK_HEADROOM="${PERF_UDP_PRESSURE_FEEDBACK_HEADROOM:-1.02}"
  PERF_UDP_PRESSURE_FEEDBACK_INTERVAL_MS="${PERF_UDP_PRESSURE_FEEDBACK_INTERVAL_MS:-500}"
  PERF_UDP_PRESSURE_FEEDBACK_INITIAL_MBIT="${PERF_UDP_PRESSURE_FEEDBACK_INITIAL_MBIT:-0}"
  PERF_UDP_PRESSURE_FEEDBACK_MAX_MBIT="${PERF_UDP_PRESSURE_FEEDBACK_MAX_MBIT:-0}"
  PERF_UDP_PRESSURE_FEEDBACK_PROBE_STEP_MBIT="${PERF_UDP_PRESSURE_FEEDBACK_PROBE_STEP_MBIT:-250}"
  PERF_UDP_PRESSURE_FEEDBACK_GOOD_RATIO="${PERF_UDP_PRESSURE_FEEDBACK_GOOD_RATIO:-0.985}"
  PERF_UDP_PRESSURE_FEEDBACK_LOW_RATIO="${PERF_UDP_PRESSURE_FEEDBACK_LOW_RATIO:-0.75}"
  PERF_UDP_PRESSURE_FEEDBACK_BACKOFF_RATIO="${PERF_UDP_PRESSURE_FEEDBACK_BACKOFF_RATIO:-0.95}"
  PERF_COLLECT_NODE_PROBES="${PERF_COLLECT_NODE_PROBES:-0}"
  PERF_KEEP_RUNNING="${PERF_KEEP_RUNNING:-0}"
  PERF_APPLY_KERNEL_TUNING="${PERF_APPLY_KERNEL_TUNING:-1}"
  PERF_SSH_RSA_COMPAT="${PERF_SSH_RSA_COMPAT:-1}"
  PERF_USER="${PERF_USER:-gatherlink}"
  PERF_REMOTE_HOME="${PERF_REMOTE_HOME:-/home/${PERF_USER}}"
  PERF_REMOTE_REPO="${PERF_REMOTE_REPO:-/home/gatherlink/src/gatherlink}"
  PERF_IPERF_TCP_CLIENT_ARGS="${PERF_IPERF_TCP_CLIENT_ARGS:-}"
  PERF_IPERF_TCP_SERVER_ARGS="${PERF_IPERF_TCP_SERVER_ARGS:-}"
  PERF_IPERF_UDP_PARALLEL="${PERF_IPERF_UDP_PARALLEL:-1}"
  OUT_DIR="${OUT_DIR:-${REPO_ROOT}/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)}"
  REPORT="${REPORT:-${OUT_DIR}/report.md}"
  REPORT_JSON="${REPORT_JSON:-${OUT_DIR}/report.json}"
  COMMAND_LOG="${COMMAND_LOG:-${OUT_DIR}/commands.log}"
  mkdir -p "${OUT_DIR}"
  : >"${COMMAND_LOG}"
}

perf_record() {
  printf '%s\n' "$*" | tee -a "${REPORT}"
}

perf_step() {
  printf '\n## %s\n\n' "$1" | tee -a "${REPORT}"
}

perf_log_cmd() {
  printf '[%s] %s\n' "$1" "$2" >>"${COMMAND_LOG}"
}

perf_remote() {
  local port="$1"
  local command="$2"
  local ssh_extra=()
  if [[ "${PERF_SSH_RSA_COMPAT:-1}" -eq 1 ]]; then
    # TODO(vm-access-hardening): The current local Hyper-V lab key is an RSA
    # Pageant-backed key. Keep this scoped to performance tooling so production
    # SSH policy is not affected, and remove it when the VM lab moves to modern
    # ed25519 host/user keys.
    ssh_extra=(-o PubkeyAcceptedAlgorithms=+ssh-rsa -o HostkeyAlgorithms=+ssh-rsa)
  fi
  perf_log_cmd "ssh-${port}" "${command}"
  ssh -n -p "${port}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${ssh_extra[@]}" "${PERF_USER}@${IP}" "${command}"
}

perf_remote_a() { perf_remote "${PORT_A}" "$1"; }
perf_remote_b() { perf_remote "${PORT_B}" "$1"; }
perf_remote_c() { perf_remote "${PORT_C}" "$1"; }

perf_path_indexes() {
  local active_paths="$1"
  python3 - "${active_paths}" <<'PY'
import sys

allowed = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
letters = [item.strip() for item in sys.argv[1].split(",") if item.strip()]
if not letters or any(letter not in allowed for letter in letters):
    raise SystemExit("--active-paths must contain one or more of a,b,c,d,e")
print(" ".join(str(allowed[letter]) for letter in letters))
PY
}

perf_path_names() {
  local active_paths="$1"
  local index
  for index in $(perf_path_indexes "${active_paths}"); do
    printf 'path-%s\n' "$(printf '%b' "\\$(printf '%03o' "$((96 + index))")")"
  done
}

perf_path_capacity_json() {
  local active_paths="$1"
  local capacity_spec="$2"
  python3 - "${active_paths}" "${capacity_spec}" <<'PY'
from __future__ import annotations

import json
import sys

allowed = {"a", "b", "c", "d", "e"}
active = [item.strip() for item in sys.argv[1].split(",") if item.strip()]
spec = sys.argv[2].strip()
if not active or any(path not in allowed for path in active):
    raise SystemExit("--active-paths must contain one or more of a,b,c,d,e")
capacities = {path: 5_000_000_000 for path in active}
if spec:
    for item in spec.split(","):
        if not item.strip():
            continue
        try:
            name, value = item.split(":", 1)
        except ValueError as error:
            raise SystemExit("--path-capacity-mbits entries must look like a:300,b:500,c:700") from error
        name = name.strip()
        if name not in active:
            raise SystemExit(f"--path-capacity-mbits contains inactive or unknown path {name!r}")
        try:
            mbit = float(value)
        except ValueError as error:
            raise SystemExit(f"invalid capacity for path {name}: {value!r}") from error
        if not mbit > 0:
            raise SystemExit(f"capacity for path {name} must be positive")
        capacities[name] = int(mbit * 1_000_000)
print(json.dumps(capacities, sort_keys=True))
PY
}

perf_apply_kernel_tuning() {
  local port
  for port in "${PORT_A}" "${PORT_B}" "${PORT_C}"; do
    perf_remote "${port}" '
      sudo sysctl -w \
        net.core.rmem_max=2147483647 \
        net.core.wmem_max=2147483647 \
        net.core.rmem_default=33554432 \
        net.core.wmem_default=33554432 >/dev/null
    '
  done
}

perf_cleanup_node() {
  local port="$1"
  perf_remote "${port}" '
    cd '"${PERF_REMOTE_REPO}"' 2>/dev/null || exit 0
    if [ -x .venv/bin/gatherlink ]; then
      .venv/bin/gatherlink services list | awk '\''/^[^[:space:]:]+[[:space:]]/ && $0 !~ /kind=remote/ {print $1}'\'' |
        while read -r service; do
          [ -n "${service}" ] && .venv/bin/gatherlink services close "${service}" >/dev/null 2>&1 || true
        done
    fi
    pkill -x iperf3 2>/dev/null || true
    sudo pkill -x wireguard-go 2>/dev/null || true
    sudo pkill -x gotatun 2>/dev/null || true
    sudo pkill -x boringtun-cli 2>/dev/null || true
    for dev in wg-perf-a wg-perf-b wg-perf-c wg-go-a wg-go-b wg-gr-a wg-gr-b wg-bc-a wg-bc-b wg-bc-c wg-ca-a wg-ca-b wgk1 wgk2 wgk3 wgk4 wgk5 wgu1 wgu2 wgu3 wgu4 wgu5 wgr1 wgr2 wgr3 wgr4 wgr5 wgb1 wgb2 wgb3 wgb4 wgb5 wg-gl-stable-a wg-gl-stable-b wg-gl-fast-a wg-gl-fast-b; do
      sudo ip link del "${dev}" 2>/dev/null || true
    done
    for path in path-a path-b path-c path-d path-e; do
      sudo tc qdisc del dev "${path}" root 2>/dev/null || true
      sudo ip link set "${path}" mtu 1500 up 2>/dev/null || true
    done
  ' >/dev/null 2>&1 || true
}

perf_cleanup_all() {
  perf_cleanup_node "${PORT_A}"
  perf_cleanup_node "${PORT_B}"
  perf_cleanup_node "${PORT_C}"
}

perf_collect_node_snapshot() {
  local label="$1"
  local port="$2"
  perf_remote "${port}" '
    printf "hostname="; hostname
    printf "kernel="; uname -r
    printf "nproc="; nproc
    printf "interfaces="; ip -br link show path-a path-b path-c path-d path-e 2>/dev/null || true
    printf "udp_snmp="; awk "/^Udp:/ {line=\$0} END {print line}" /proc/net/snmp
    printf "rmem_max="; cat /proc/sys/net/core/rmem_max 2>/dev/null || true
    printf "wmem_max="; cat /proc/sys/net/core/wmem_max 2>/dev/null || true
  ' >"${OUT_DIR}/${label}-node-snapshot.txt" || true
}

perf_start_node_probe() {
  local label="$1"
  local port="$2"
  local seconds="$3"
  perf_remote "${port}" "cd ${PERF_REMOTE_REPO} && rm -f /tmp/${label}.perf.json && setsid -f .venv/bin/python tools/hyperv/vm_perf_probe.py --duration ${seconds} --interval 0.5 --out /tmp/${label}.perf.json --match gatherlink --match udp-pressure --match python --match iperf3 --match wireguard-go --match gotatun --match boringtun-cli --netdev path-a --netdev path-b --netdev path-c --netdev path-d --netdev path-e >/tmp/${label}.perf.log 2>&1 < /dev/null"
}

perf_fetch_node_probe() {
  local label="$1"
  local port="$2"
  local attempt
  for attempt in 1 2 3 4 5; do
    if perf_remote "${port}" "test -s /tmp/${label}.perf.json" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  perf_remote "${port}" "cat /tmp/${label}.perf.json 2>/dev/null || true" >"${OUT_DIR}/${label}.perf.json" || true
}

perf_run_iperf_tcp() {
  local label="$1"
  local server_port="$2"
  local client_port="$3"
  local bind_addr="$4"
  local target_addr="$5"
  local port_number="$6"
  local parallel="$7"
  local seconds="$8"

  perf_remote "${server_port}" "pkill -x iperf3 2>/dev/null || true; iperf3 -s -D -1 -B ${bind_addr} -p ${port_number} ${PERF_IPERF_TCP_SERVER_ARGS} --logfile /tmp/${label}.iperf-server.log"
  sleep 1
  perf_remote "${client_port}" "iperf3 -c ${target_addr} -p ${port_number} -P ${parallel} -t ${seconds} ${PERF_IPERF_TCP_CLIENT_ARGS} --json" \
    >"${OUT_DIR}/${label}.json" 2>"${OUT_DIR}/${label}.stderr" || true
  perf_remote "${server_port}" "cat /tmp/${label}.iperf-server.log 2>/dev/null || true" >"${OUT_DIR}/${label}.server.log" || true
}

perf_run_iperf_udp() {
  local label="$1"
  local server_port="$2"
  local client_port="$3"
  local bind_addr="$4"
  local target_addr="$5"
  local port_number="$6"
  local rate="$7"
  local length="$8"
  local seconds="$9"

  perf_remote "${server_port}" "pkill -x iperf3 2>/dev/null || true; iperf3 -s -D -1 -B ${bind_addr} -p ${port_number} --logfile /tmp/${label}.iperf-server.log"
  sleep 1
  perf_remote "${client_port}" "iperf3 -c ${target_addr} -p ${port_number} -u -b ${rate} -l ${length} -t ${seconds} --json" \
    >"${OUT_DIR}/${label}.json" 2>"${OUT_DIR}/${label}.stderr" || true
  perf_remote "${server_port}" "cat /tmp/${label}.iperf-server.log 2>/dev/null || true" >"${OUT_DIR}/${label}.server.log" || true
}

perf_start_iperf_tcp_server() {
  local label="$1"
  local server_port="$2"
  local bind_addr="$3"
  local port_number="$4"

  perf_remote "${server_port}" "iperf3 -s -D -1 -B ${bind_addr} -p ${port_number} ${PERF_IPERF_TCP_SERVER_ARGS} --logfile /tmp/${label}.iperf-server.log"
}

perf_start_iperf_tcp_client_background() {
  local label="$1"
  local client_port="$2"
  local target_addr="$3"
  local port_number="$4"
  local parallel="$5"
  local seconds="$6"

  perf_remote "${client_port}" "rm -f /tmp/${label}.json /tmp/${label}.stderr /tmp/${label}.pid; setsid -f sh -c 'iperf3 -c ${target_addr} -p ${port_number} -P ${parallel} -t ${seconds} ${PERF_IPERF_TCP_CLIENT_ARGS} --json >/tmp/${label}.json 2>/tmp/${label}.stderr' >/dev/null 2>&1; echo \$! >/tmp/${label}.pid"
}

perf_fetch_iperf_tcp_background() {
  local label="$1"
  local server_port="$2"
  local client_port="$3"

  perf_wait_remote_file "${client_port}" "/tmp/${label}.json" 30
  perf_remote "${client_port}" "cat /tmp/${label}.json 2>/dev/null || true" >"${OUT_DIR}/${label}.json" || true
  perf_remote "${client_port}" "cat /tmp/${label}.stderr 2>/dev/null || true" >"${OUT_DIR}/${label}.stderr" || true
  perf_remote "${server_port}" "cat /tmp/${label}.iperf-server.log 2>/dev/null || true" >"${OUT_DIR}/${label}.server.log" || true
}

perf_start_iperf_udp_server() {
  local label="$1"
  local server_port="$2"
  local bind_addr="$3"
  local port_number="$4"

  perf_remote "${server_port}" "iperf3 -s -D -1 -B ${bind_addr} -p ${port_number} --logfile /tmp/${label}.iperf-server.log"
}

perf_start_iperf_udp_client_background() {
  local label="$1"
  local client_port="$2"
  local target_addr="$3"
  local port_number="$4"
  local rate="$5"
  local length="$6"
  local seconds="$7"

  perf_remote "${client_port}" "rm -f /tmp/${label}.json /tmp/${label}.stderr /tmp/${label}.pid; setsid -f sh -c 'iperf3 -c ${target_addr} -p ${port_number} -u -P ${PERF_IPERF_UDP_PARALLEL} -b ${rate} -l ${length} -t ${seconds} --json >/tmp/${label}.json 2>/tmp/${label}.stderr' >/dev/null 2>&1; echo \$! >/tmp/${label}.pid"
}

perf_fetch_iperf_udp_background() {
  local label="$1"
  local server_port="$2"
  local client_port="$3"

  perf_wait_remote_file "${client_port}" "/tmp/${label}.json" 30
  perf_remote "${client_port}" "cat /tmp/${label}.json 2>/dev/null || true" >"${OUT_DIR}/${label}.json" || true
  perf_remote "${client_port}" "cat /tmp/${label}.stderr 2>/dev/null || true" >"${OUT_DIR}/${label}.stderr" || true
  perf_remote "${server_port}" "cat /tmp/${label}.iperf-server.log 2>/dev/null || true" >"${OUT_DIR}/${label}.server.log" || true
}

perf_wait_remote_file() {
  local port="$1"
  local remote_path="$2"
  local timeout_seconds="$3"
  local elapsed=0

  # High-BDP mixed WireGuard tests can finish iperf traffic before the JSON
  # result is flushed to disk. Wait for a non-empty result so benchmark scripts
  # do not silently drop the TCP half of a mixed run.
  while [[ "${elapsed}" -lt "${timeout_seconds}" ]]; do
    if perf_remote "${port}" "test -s '${remote_path}'" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 0
}

perf_compile_udp_pressure() {
  local port="$1"
  perf_remote "${port}" "cd ${PERF_REMOTE_REPO} && rustc --edition 2021 -O tools/udp_pressure.rs -o /tmp/gatherlink-udp-pressure"
}

perf_start_udp_pressure_sink() {
  local label="$1"
  local server_port="$2"
  local bind_addr="$3"
  local duration="$4"
  local feedback_target="${5:-}"

  local feedback_arg=""
  if [[ "${PERF_UDP_PRESSURE_FEEDBACK}" -eq 1 && -n "${feedback_target}" ]]; then
    feedback_arg="--feedback-target ${feedback_target} --feedback-interval-ms ${PERF_UDP_PRESSURE_FEEDBACK_INTERVAL_MS}"
  fi
  local truncate_arg=""
  if [[ "${PERF_UDP_PRESSURE_RECV_TRUNCATE}" -eq 1 ]]; then
    truncate_arg="--recv-truncate"
  fi
  local taskset_arg=""
  if [[ -n "${PERF_UDP_PRESSURE_SINK_CPUSET}" ]]; then
    taskset_arg="taskset -c ${PERF_UDP_PRESSURE_SINK_CPUSET}"
  fi
  perf_remote "${server_port}" "rm -f /tmp/${label}.json /tmp/${label}.progress /tmp/${label}.stderr /tmp/${label}.pid; nohup ${taskset_arg} /tmp/gatherlink-udp-pressure sink --bind ${bind_addr} --duration $((duration + 10)) --idle-after-first 2 --out /tmp/${label}.progress --workers ${PERF_UDP_PRESSURE_WORKERS} --bind-port-stride ${PERF_UDP_PRESSURE_PORT_STRIDE} --recv-batch ${PERF_UDP_PRESSURE_RECV_BATCH} --recv-buffer-size ${PERF_UDP_PRESSURE_RECV_BUFFER_SIZE} ${truncate_arg} ${feedback_arg} >/tmp/${label}.json 2>/tmp/${label}.stderr < /dev/null & echo \$! >/tmp/${label}.pid"
}

perf_start_udp_pressure_client_background() {
  local label="$1"
  local client_port="$2"
  local target_addr="$3"
  local duration="$4"
  local payload_size="$5"
  local target_mbit="$6"
  local feedback_bind="${7:-}"

  local rate_arg=""
  if [[ -n "${target_mbit}" ]]; then
    rate_arg="--target-mbit ${target_mbit}"
  fi
  local feedback_arg=""
  if [[ "${PERF_UDP_PRESSURE_FEEDBACK}" -eq 1 && -n "${feedback_bind}" ]]; then
    feedback_arg="--feedback-bind ${feedback_bind} --feedback-headroom ${PERF_UDP_PRESSURE_FEEDBACK_HEADROOM} --feedback-initial-mbit ${PERF_UDP_PRESSURE_FEEDBACK_INITIAL_MBIT} --feedback-max-mbit ${PERF_UDP_PRESSURE_FEEDBACK_MAX_MBIT} --feedback-probe-step-mbit ${PERF_UDP_PRESSURE_FEEDBACK_PROBE_STEP_MBIT} --feedback-good-ratio ${PERF_UDP_PRESSURE_FEEDBACK_GOOD_RATIO} --feedback-low-ratio ${PERF_UDP_PRESSURE_FEEDBACK_LOW_RATIO} --feedback-backoff-ratio ${PERF_UDP_PRESSURE_FEEDBACK_BACKOFF_RATIO}"
  fi
  local taskset_arg=""
  if [[ -n "${PERF_UDP_PRESSURE_SEND_CPUSET}" ]]; then
    taskset_arg="taskset -c ${PERF_UDP_PRESSURE_SEND_CPUSET}"
  fi
  perf_remote "${client_port}" "rm -f /tmp/${label}.json /tmp/${label}.stderr /tmp/${label}.pid; setsid -f sh -c '${taskset_arg} /tmp/gatherlink-udp-pressure send --target ${target_addr} --duration ${duration} --payload-size ${payload_size} ${rate_arg} --flows ${PERF_UDP_PRESSURE_FLOWS} --target-port-stride ${PERF_UDP_PRESSURE_PORT_STRIDE} --send-batch ${PERF_UDP_PRESSURE_SEND_BATCH} --udp-gso-segments ${PERF_UDP_PRESSURE_GSO_SEGMENTS} ${feedback_arg} >/tmp/${label}.json 2>/tmp/${label}.stderr' >/dev/null 2>&1; echo \$! >/tmp/${label}.pid"
}

perf_fetch_udp_pressure_background() {
  local label="$1"
  local server_port="$2"
  local client_port="$3"

  perf_wait_remote_file "${client_port}" "/tmp/${label}.json" 20 || true
  perf_wait_remote_file "${server_port}" "/tmp/${label}.json" 20 || true
  perf_remote "${client_port}" "cat /tmp/${label}.json 2>/dev/null || true" >"${OUT_DIR}/${label}-generator.json" || true
  perf_remote "${client_port}" "cat /tmp/${label}.stderr 2>/dev/null || true" >"${OUT_DIR}/${label}-generator.stderr" || true
  perf_remote "${server_port}" "cat /tmp/${label}.json 2>/dev/null || cat /tmp/${label}.progress 2>/dev/null || true" >"${OUT_DIR}/${label}-sink.json" || true
  perf_remote "${server_port}" "cat /tmp/${label}.progress 2>/dev/null || true" >"${OUT_DIR}/${label}-sink-progress.json" || true
  perf_remote "${server_port}" "cat /tmp/${label}.stderr 2>/dev/null || true" >"${OUT_DIR}/${label}-sink.stderr" || true
}

perf_summarize_iperf_jsons() {
  python3 - "$OUT_DIR" "$REPORT_JSON" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
report_json = Path(sys.argv[2])
results = []
for path in sorted(out_dir.glob("*.json")):
    if path.name == "report.json":
        continue
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        continue
    if "error" in data:
        results.append(
            {
                "name": path.stem,
                "bits_per_second": 0.0,
                "mbit_per_second": 0.0,
                "error": str(data.get("error") or "unknown iperf error"),
            }
        )
        continue
    if "bits_per_second" in data and "packets" in data:
        bps = float(data.get("bits_per_second") or 0)
        result = {
            "name": path.stem,
            "bits_per_second": bps,
            "mbit_per_second": bps / 1_000_000,
            "packets": int(data.get("packets") or 0),
            "bytes": int(data.get("bytes") or 0),
            "complete": bool(data.get("complete", False)),
        }
        for key in ("send_calls", "recv_calls", "max_send_batch", "max_recv_batch"):
            if key in data:
                result[key] = int(data.get(key) or 0)
        results.append(result)
        continue
    end = data.get("end", {})
    if not isinstance(end, dict) or not end:
        continue
    sent = end.get("sum_sent") or end.get("sum") or {}
    received = end.get("sum_received") or end.get("sum") or {}
    bps = float(received.get("bits_per_second") or sent.get("bits_per_second") or 0)
    result = {
        "name": path.stem,
        "bits_per_second": bps,
        "mbit_per_second": bps / 1_000_000,
    }
    if "lost_percent" in received:
        result["lost_percent"] = float(received["lost_percent"])
    elif "lost_percent" in sent:
        result["lost_percent"] = float(sent["lost_percent"])
    if "retransmits" in sent:
        result["retransmits"] = int(sent["retransmits"])
    results.append(result)
report = {
    "generated_utc": out_dir.name.split("-")[0],
    "output": str(out_dir),
    "results": results,
}
report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
for result in results:
    extras = []
    if "lost_percent" in result:
        extras.append(f"lost={result['lost_percent']:.2f}%")
    if "retransmits" in result:
        extras.append(f"retrans={result['retransmits']}")
    if "error" in result:
        extras.append(f"error={result['error']}")
    suffix = f" {' '.join(extras)}" if extras else ""
    print(f"- {result['name']}: {result['mbit_per_second']:.2f} Mbit/s{suffix}")
PY
}
