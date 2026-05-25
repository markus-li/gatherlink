#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
PORT_C="2203"
DURATION=20
PAYLOAD_SIZE=1400
TARGET_MBIT="1000"
PATH_MTU=1472
CORE_BATCH_SIZE=512
PATH_CAPACITY_MBIT=5000
ACTIVE_PATHS="a"
SCHEDULER_MODE="round_robin"
FLOWLET_IDLE_US=0
FLOWLET_MAX_HOLD_US=0
REORDER_HOLD_US=2000
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-relay-rust-udp"
KEEP_RUNNING=0

usage() {
  cat <<'USAGE'
Usage: run_relay_rust_udp_speed.sh [options]

Starts the production Gatherlink B -> C -> A relay path and uses the compiled
tools/udp_pressure.rs helper as the raw UDP application. This avoids iperf3's
TCP control channel and avoids measuring Python packet-generator overhead.

Options:
  --ip IP              Management IP used with the WSL portproxy setup. Default 172.22.0.1.
  --port-a PORT        SSH port for VM A. Default 2201.
  --port-b PORT        SSH port for VM B. Default 2202.
  --port-c PORT        SSH port for VM C. Default 2203.
  --duration SECONDS   UDP send duration. Default 20.
  --payload-size BYTES UDP payload size. Default 1400.
  --target-mbit MBIT   Pace the UDP generator to this decimal Mbit/s. Default 1000.
  --path-mtu BYTES     Gatherlink path MTU in generated configs. Default 1472.
  --core-batch-size N  Core runner batch size passed to gatherlink run start. Default 512.
  --path-capacity-mbit MBIT
                      Static per-path scheduler capacity hint. Default 5000.
  --active-paths LIST  Comma-separated path letters to configure. Default a.
  --scheduler-mode MODE
                      Python-selected scheduler mode compiled for Rust. Default round_robin.
  --flowlet-idle-us N  Stick one service/source to a path until idle for N us. Default 0.
  --flowlet-max-hold-us N
                      Maximum continuous service/source path hold in us. Default 0.
  --reorder-hold-us N  Receiver hold time for path reordering in us. Default 2000.
  --out DIR            Report directory.
  --keep-running       Leave Gatherlink relay/core services running for inspection.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --port-a) PORT_A="$2"; shift 2 ;;
    --port-b) PORT_B="$2"; shift 2 ;;
    --port-c) PORT_C="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --payload-size) PAYLOAD_SIZE="$2"; shift 2 ;;
    --target-mbit) TARGET_MBIT="$2"; shift 2 ;;
    --path-mtu) PATH_MTU="$2"; shift 2 ;;
    --core-batch-size) CORE_BATCH_SIZE="$2"; shift 2 ;;
    --path-capacity-mbit) PATH_CAPACITY_MBIT="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --scheduler-mode) SCHEDULER_MODE="$2"; shift 2 ;;
    --flowlet-idle-us) FLOWLET_IDLE_US="$2"; shift 2 ;;
    --flowlet-max-hold-us) FLOWLET_MAX_HOLD_US="$2"; shift 2 ;;
    --reorder-hold-us) REORDER_HOLD_US="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "${OUT_DIR}"
REPORT="${OUT_DIR}/report.md"
REPORT_JSON="${OUT_DIR}/report.json"
: >"${REPORT}"

record() {
  printf '%s\n' "$*" | tee -a "${REPORT}"
}

remote() {
  local port="$1"
  local command="$2"
  ssh -n -p "${port}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new gatherlink@"${IP}" "${command}"
}

remote_a() { remote "${PORT_A}" "$1"; }
remote_b() { remote "${PORT_B}" "$1"; }
remote_c() { remote "${PORT_C}" "$1"; }

fetch_probe() {
  local port="$1"
  local remote_path="$2"
  local local_path="$3"

  for _ in 1 2 3 4 5 6 7 8; do
    if remote "${port}" "test -s '${remote_path}'" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  remote "${port}" "cat '${remote_path}' 2>/dev/null || true" >"${local_path}" || true
}

cleanup_tool() {
  remote_a "if [ -f /tmp/relayrust-sink.pid ]; then kill \"\$(cat /tmp/relayrust-sink.pid)\" 2>/dev/null || true; fi; rm -f /tmp/relayrust-sink.pid /tmp/relayrust-sink.out /tmp/relayrust-sink.err /tmp/relayrust-sink.progress" >/dev/null 2>&1 || true
}

cleanup_gatherlink() {
  "${SCRIPT_DIR}/run_relay_udp_speed.sh" \
    --ip "${IP}" \
    --port-a "${PORT_A}" \
    --port-b "${PORT_B}" \
    --port-c "${PORT_C}" \
    --duration 1 \
    --payload-size 100 \
    --target-mbit 1 \
    --active-paths "${ACTIVE_PATHS}" \
    --path-capacity-mbit "${PATH_CAPACITY_MBIT}" \
    --out "${OUT_DIR}/cleanup" >/dev/null 2>&1 || true
}

cleanup() {
  cleanup_tool
  cleanup_gatherlink
}

if [[ "${KEEP_RUNNING}" -eq 0 ]]; then
  trap cleanup EXIT
fi

record "# Gatherlink Relay Rust UDP Pressure Speed"
record ""
record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
record "- duration_seconds: ${DURATION}"
record "- payload_size: ${PAYLOAD_SIZE}"
record "- target_mbit: ${TARGET_MBIT}"
record "- path_mtu: ${PATH_MTU}"
record "- core_batch_size: ${CORE_BATCH_SIZE}"
record "- path_capacity_mbit: ${PATH_CAPACITY_MBIT}"
record "- active_paths: ${ACTIVE_PATHS}"
record "- scheduler_mode: ${SCHEDULER_MODE}"
record "- flowlet_idle_us: ${FLOWLET_IDLE_US}"
record "- flowlet_max_hold_us: ${FLOWLET_MAX_HOLD_US}"
record "- reorder_hold_us: ${REORDER_HOLD_US}"
record "- out: ${OUT_DIR}"
record ""

for port in "${PORT_A}" "${PORT_B}"; do
  remote "${port}" "cd /home/gatherlink/src/gatherlink && rustc --edition 2021 -O tools/udp_pressure.rs -o /tmp/gatherlink-udp-pressure"
done

"${SCRIPT_DIR}/run_relay_udp_speed.sh" \
  --ip "${IP}" \
  --port-a "${PORT_A}" \
  --port-b "${PORT_B}" \
  --port-c "${PORT_C}" \
  --duration "${DURATION}" \
  --payload-size "${PAYLOAD_SIZE}" \
  --target-mbit 1 \
  --path-mtu "${PATH_MTU}" \
  --core-batch-size "${CORE_BATCH_SIZE}" \
  --path-capacity-mbit "${PATH_CAPACITY_MBIT}" \
  --active-paths "${ACTIVE_PATHS}" \
  --scheduler-mode "${SCHEDULER_MODE}" \
  --flowlet-idle-us "${FLOWLET_IDLE_US}" \
  --flowlet-max-hold-us "${FLOWLET_MAX_HOLD_US}" \
  --reorder-hold-us "${REORDER_HOLD_US}" \
  --setup-only \
  --out "${OUT_DIR}/setup" >/dev/null

cleanup_tool
remote_a "nohup /tmp/gatherlink-udp-pressure sink --bind 127.0.0.1:19091 --duration $((DURATION + 10)) --idle-after-first 2 --out /tmp/relayrust-sink.progress >/tmp/relayrust-sink.out 2>/tmp/relayrust-sink.err < /dev/null & echo \$! >/tmp/relayrust-sink.pid"
sleep 1
probe_duration=$((DURATION + 6))
remote_a "cd /home/gatherlink/src/gatherlink && rm -f /tmp/relayrust-node-a.perf.json && setsid -f .venv/bin/python tools/hyperv/vm_perf_probe.py --duration ${probe_duration} --interval 0.5 --out /tmp/relayrust-node-a.perf.json --match gatherlink --match python --match iperf3 --netdev path-a --netdev path-b --netdev path-c >/tmp/relayrust-node-a.perf.log 2>&1 < /dev/null" || true
remote_b "cd /home/gatherlink/src/gatherlink && rm -f /tmp/relayrust-node-b.perf.json && setsid -f .venv/bin/python tools/hyperv/vm_perf_probe.py --duration ${probe_duration} --interval 0.5 --out /tmp/relayrust-node-b.perf.json --match gatherlink --match python --match iperf3 --netdev path-a --netdev path-b --netdev path-c >/tmp/relayrust-node-b.perf.log 2>&1 < /dev/null" || true
remote_c "cd /home/gatherlink/src/gatherlink && rm -f /tmp/relayrust-node-c.perf.json && setsid -f .venv/bin/python tools/hyperv/vm_perf_probe.py --duration ${probe_duration} --interval 0.5 --out /tmp/relayrust-node-c.perf.json --match gatherlink --match python --match iperf3 --netdev path-a --netdev path-b --netdev path-c >/tmp/relayrust-node-c.perf.log 2>&1 < /dev/null" || true
remote_b "/tmp/gatherlink-udp-pressure send --target 127.0.0.1:55180 --duration ${DURATION} --payload-size ${PAYLOAD_SIZE} --target-mbit ${TARGET_MBIT}" \
  >"${OUT_DIR}/generator.json"
sleep 3
remote_a "cat /tmp/relayrust-sink.out 2>/dev/null || true" >"${OUT_DIR}/sink.json" || true
remote_a "cat /tmp/relayrust-sink.progress 2>/dev/null || true" >"${OUT_DIR}/sink-progress.json" || true
remote_a "cat /tmp/relayrust-sink.err 2>/dev/null || true" >"${OUT_DIR}/sink.err" || true
remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status relayudp.vm.node-b" >"${OUT_DIR}/status-b.json" || true
remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status relayudp.vm.node-a" >"${OUT_DIR}/status-a.json" || true
fetch_probe "${PORT_A}" /tmp/relayrust-node-a.perf.json "${OUT_DIR}/node-a.perf.json"
fetch_probe "${PORT_B}" /tmp/relayrust-node-b.perf.json "${OUT_DIR}/node-b.perf.json"
fetch_probe "${PORT_C}" /tmp/relayrust-node-c.perf.json "${OUT_DIR}/node-c.perf.json"

python3 - "${OUT_DIR}/generator.json" "${OUT_DIR}/sink.json" "${OUT_DIR}/sink-progress.json" "${REPORT_JSON}" <<'PY' | tee -a "${REPORT}"
from __future__ import annotations

import json
import sys
from pathlib import Path

generator = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sink_text = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
if not sink_text:
    sink_text = Path(sys.argv[3]).read_text(encoding="utf-8").strip()
sink = json.loads(sink_text) if sink_text else {}
results = [
    {
        "name": "generator",
        "bits_per_second": float(generator["bits_per_second"]),
        "mbit_per_second": float(generator["bits_per_second"]) / 1_000_000,
        "packets": int(generator["packets"]),
        "bytes": int(generator["bytes"]),
    }
]
print("## Results")
print(
    f"generator: {generator['bits_per_second'] / 1_000_000:.2f} Mbit/s "
    f"packets={generator['packets']} bytes={generator['bytes']}"
)
if sink:
    delta = int(generator["packets"]) - int(sink["packets"])
    print(
        f"sink: {sink['bits_per_second'] / 1_000_000:.2f} Mbit/s "
        f"packets={sink['packets']} bytes={sink['bytes']}"
    )
    print(f"application_packet_delta: {delta}")
    results.append(
        {
            "name": "sink",
            "bits_per_second": float(sink["bits_per_second"]),
            "mbit_per_second": float(sink["bits_per_second"]) / 1_000_000,
            "packets": int(sink["packets"]),
            "bytes": int(sink["bytes"]),
            "application_packet_delta": delta,
        }
    )
else:
    print("sink: no result")
Path(sys.argv[4]).write_text(json.dumps({"results": results}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

record ""
record "Report: ${REPORT}"
if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
  record "Gatherlink relay services were left up for inspection."
fi
printf '\nGatherlink relay Rust UDP pressure speed complete.\nReport: %s\n' "${REPORT}"
