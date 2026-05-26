#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/perf_common.sh"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
DURATION=20
PAYLOAD_SIZE=1100
TARGET_MBIT=""
LINK_MTU=1500
PATH_MTU=1472
CORE_BATCH_SIZE=512
SCHEDULER_REAPPLY_INTERVAL=""
SECURITY_MODE="authenticated"
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-gatherlink-onehop-udp"
REPORT_JSON=""
KEEP_RUNNING=0
SETUP_ONLY=0
CLEANUP_ONLY=0
APPLY_KERNEL_TUNING=1
ACTIVE_PATHS="a,b,c"
SCHEDULER_MODE="round_robin"
SCHEDULER_TRAFFIC_BIAS="auto"
PATH_CAPACITY_MBITS=""
FLOWLET_IDLE_US=0
FLOWLET_MAX_HOLD_US=0
PATH_RUN_DATAGRAMS=0
REORDER_HOLD_US=2000
SHAPE_PROFILE="clean"
COMPETING_RATE=""
COMPETING_LENGTH=1200

usage() {
  cat <<'USAGE'
Usage: run_gatherlink_onehop_speed.sh [options]

Runs raw application UDP through direct two-node Gatherlink carrier sockets:

  UDP generator on VM B -> Gatherlink core on B -> direct carrier path(s) ->
  Gatherlink core on VM A -> UDP sink on VM A

This deliberately excludes the relay VM. Use it to isolate endpoint Gatherlink
transport cost from untrusted relay forwarding cost.

Options:
  --ip IP              Management IP used with the WSL portproxy setup. Default 172.22.0.1.
  --port-a PORT        SSH port for VM A. Default 2201.
  --port-b PORT        SSH port for VM B. Default 2202.
  --duration SECONDS   UDP send duration. Default 20.
  --payload-size BYTES UDP payload size. Default 1100.
  --target-mbit MBIT   Pace the UDP generator to this decimal Mbit/s. Default unlimited.
  --link-mtu BYTES      Linux path interface MTU on VM A and VM B. Default 1500.
  --path-mtu BYTES      Gatherlink path MTU in generated configs. Default 1472.
  --core-batch-size N   Core runner batch size passed to gatherlink run start. Default 512.
  --scheduler-reapply-interval SECONDS
                        Enable live Python scheduler reapply at this cadence. Default disabled.
  --security-mode MODE  Gatherlink security mode for generated configs: authenticated or none. Default authenticated.
  --active-paths LIST   Comma-separated path letters to configure. Default a,b,c; supports a,b,c,d,e.
  --scheduler-mode MODE  Python-selected scheduler mode compiled for Rust. Default round_robin.
  --scheduler-traffic-bias BIAS
                        Bias coordinated_adaptive toward auto, tcp, or udp. Default auto.
  --path-capacity-mbits SPEC
                        Static per-path scheduler capacity hints, for example a:300,b:500,c:700,d:220,e:210.
                        If omitted, every active path uses 5000 Mbit/s.
  --flowlet-idle-us N   Stick one service/source to a path until idle for N us. Default 0.
  --flowlet-max-hold-us N
                        Maximum continuous service/source path hold in us. Default 0.
  --path-run-datagrams N
                        Maximum hot-burst datagrams per path before rescheduling. Default 0.
  --reorder-hold-us N   Receiver hold time for path reordering in us. Default 2000.
  --shape-profile NAME  Hyper-V path shaping profile. Default clean.
  --competing-rate RATE Start direct UDP competitors on every active path while Gatherlink sends.
  --competing-length BYTES
                        UDP block size for competing traffic. Default 1200.
  --out DIR             Report directory.
  --keep-running        Leave Gatherlink services running for inspection.
  --setup-only          Start core services but do not run the temporary UDP sink/generator.
  --cleanup-only        Stop generated one-hop services and temporary WireGuard links, then exit.
  --skip-kernel-tuning  Do not apply the lab UDP socket-buffer sysctls before the run.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --port-a) PORT_A="$2"; shift 2 ;;
    --port-b) PORT_B="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --payload-size) PAYLOAD_SIZE="$2"; shift 2 ;;
    --target-mbit) TARGET_MBIT="$2"; shift 2 ;;
    --link-mtu) LINK_MTU="$2"; shift 2 ;;
    --path-mtu) PATH_MTU="$2"; shift 2 ;;
    --core-batch-size) CORE_BATCH_SIZE="$2"; shift 2 ;;
    --scheduler-reapply-interval) SCHEDULER_REAPPLY_INTERVAL="$2"; shift 2 ;;
    --security-mode) SECURITY_MODE="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --scheduler-mode) SCHEDULER_MODE="$2"; shift 2 ;;
    --scheduler-traffic-bias) SCHEDULER_TRAFFIC_BIAS="$2"; shift 2 ;;
    --path-capacity-mbits) PATH_CAPACITY_MBITS="$2"; shift 2 ;;
    --flowlet-idle-us) FLOWLET_IDLE_US="$2"; shift 2 ;;
    --flowlet-max-hold-us) FLOWLET_MAX_HOLD_US="$2"; shift 2 ;;
    --path-run-datagrams) PATH_RUN_DATAGRAMS="$2"; shift 2 ;;
    --reorder-hold-us) REORDER_HOLD_US="$2"; shift 2 ;;
    --shape-profile) SHAPE_PROFILE="$2"; shift 2 ;;
    --competing-rate) COMPETING_RATE="$2"; shift 2 ;;
    --competing-length) COMPETING_LENGTH="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --setup-only) SETUP_ONLY=1; KEEP_RUNNING=1; shift ;;
    --cleanup-only) CLEANUP_ONLY=1; shift ;;
    --skip-kernel-tuning) APPLY_KERNEL_TUNING=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "${OUT_DIR}"
REPORT="${OUT_DIR}/report.md"
REPORT_JSON="${OUT_DIR}/report.json"
: >"${REPORT}"

ACTIVE_PATH_LETTERS="${ACTIVE_PATHS//,/ }"
ACTIVE_PATH_INDICES_PY="$(perf_path_indexes "${ACTIVE_PATHS}" | tr ' ' ',' | sed 's/,/, /g')"
PATH_CAPACITY_JSON="$(perf_path_capacity_json "${ACTIVE_PATHS}" "${PATH_CAPACITY_MBITS}")"
if [[ "${SECURITY_MODE}" != "authenticated" && "${SECURITY_MODE}" != "none" ]]; then
  echo "--security-mode must be authenticated or none" >&2
  exit 2
fi
if [[ -n "${SCHEDULER_REAPPLY_INTERVAL}" ]]; then
  python3 - "${SCHEDULER_REAPPLY_INTERVAL}" <<'PY'
from __future__ import annotations

import sys

try:
    interval = float(sys.argv[1])
except ValueError as exc:
    raise SystemExit("--scheduler-reapply-interval must be numeric") from exc
if interval <= 0:
    raise SystemExit("--scheduler-reapply-interval must be greater than zero")
PY
fi

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

cleanup_node() {
  local port="$1"
  remote "${port}" '
    cd /home/gatherlink/src/gatherlink 2>/dev/null || exit 0
    if [ -x .venv/bin/gatherlink ]; then
      .venv/bin/gatherlink services list | awk '\''/^[^[:space:]:]+[[:space:]]/ && $0 !~ /kind=remote/ {print $1}'\'' |
        while read -r service; do
          [ -n "${service}" ] && .venv/bin/gatherlink services close "${service}" >/dev/null 2>&1 || true
        done
    fi
    # Benchmark reruns must not inherit stale one-hop services. The normal
    # service registry is still the first stop path, but earlier broken runs can
    # leave orphaned children with stopped registry records. Keep this kill
    # pattern scoped to the generated one-hop temp configs.
    pkill -f "gatherlink.cli.main run service /tmp/gl-onehop-node" 2>/dev/null || true
    if [ -f /tmp/gl-onehop-sink.pid ]; then
      kill "$(cat /tmp/gl-onehop-sink.pid)" 2>/dev/null || true
    fi
    for dev in wg-go-a wg-go-b wg-gr-a wg-gr-b wg-perf-a wg-perf-b; do
      sudo ip link del "${dev}" 2>/dev/null || true
    done
    rm -f /tmp/gl-onehop-sink.out /tmp/gl-onehop-sink.err /tmp/gl-onehop-sink.progress /tmp/gl-onehop-sink.pid
    for path in path-a path-b path-c path-d path-e; do
      sudo tc qdisc del dev "${path}" root 2>/dev/null || true
      sudo ip link set "${path}" up 2>/dev/null || true
    done
  ' >/dev/null 2>&1 || true
}

cleanup() {
  cleanup_node "${PORT_A}"
  cleanup_node "${PORT_B}"
}

if [[ "${CLEANUP_ONLY}" -eq 1 ]]; then
  record "# Gatherlink Raw UDP One-Hop Cleanup"
  record ""
  record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  record "- out: ${OUT_DIR}"
  record ""
  cleanup
  record "Generated one-hop services, temporary WireGuard links, and path qdisc state were cleaned up."
  printf '\nGatherlink raw UDP one-hop cleanup complete.\nReport: %s\n' "${REPORT}"
  exit 0
fi

apply_link_mtu() {
  local port="$1"
  remote "${port}" "
    for path in ${ACTIVE_PATH_LETTERS}; do
      sudo ip link set \"path-\${path}\" mtu ${LINK_MTU} up
    done
  "
}

apply_kernel_tuning() {
  local port
  for port in "${PORT_A}" "${PORT_B}"; do
    remote "${port}" '
      sudo sysctl -w \
        net.core.rmem_max=2147483647 \
        net.core.wmem_max=2147483647 \
        net.core.rmem_default=33554432 \
        net.core.wmem_default=33554432 >/dev/null
    '
  done
}

if [[ "${KEEP_RUNNING}" -eq 0 && "${SETUP_ONLY}" -eq 0 ]]; then
  trap cleanup EXIT
fi

write_configs() {
  remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

capacity_bps_by_path = ${PATH_CAPACITY_JSON}
paths = []
for index in [${ACTIVE_PATH_INDICES_PY}]:
    path_name = f'path-{chr(96 + index)}'
    capacity_bps = capacity_bps_by_path[chr(96 + index)]
    paths.append(
        {
            'name': path_name,
            'interface': path_name,
            'transport_bind': f'10.91.{index}.11:{61100 + index}',
            'transport_remote': f'10.91.{index}.12:{61000 + index}',
            'scheduler': {
                'mtu': ${PATH_MTU},
                'tx_capacity_bps': capacity_bps,
                'rx_capacity_bps': capacity_bps,
                'reorder_hold_us': ${REORDER_HOLD_US},
            },
        }
    )

security_mode = '${SECURITY_MODE}'
security = {'mode': 'none'}
if security_mode == 'authenticated':
    security = {
        'mode': 'authenticated',
        'local_receiver_index': 2601,
        'remote_receiver_index': 2501,
        'send_key': key(0x82),
        'receive_key': key(0x81),
    }

cfg = {
    'schema_version': 1,
    'node': 'onehop-node-a-sink',
    'role': 'server',
    'peer': 'onehop-node-b-source',
    'scheduler': {'mode': '${SCHEDULER_MODE}', 'traffic_bias': '${SCHEDULER_TRAFFIC_BIAS}'},
    'paths': paths,
    'services': [
        {
            'name': 'udp-speed',
            'listen': '0.0.0.0:0',
            'target': '127.0.0.1:19091',
            'return_mode': 'fixed',
            'scheduler_flowlet_idle_us': ${FLOWLET_IDLE_US},
            'scheduler_flowlet_max_hold_us': ${FLOWLET_MAX_HOLD_US},
            'scheduler_path_run_datagrams': ${PATH_RUN_DATAGRAMS},
        }
    ],
    'security': security,
}
Path('/tmp/gl-onehop-node-a.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"

  remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

capacity_bps_by_path = ${PATH_CAPACITY_JSON}
paths = []
for index in [${ACTIVE_PATH_INDICES_PY}]:
    path_name = f'path-{chr(96 + index)}'
    capacity_bps = capacity_bps_by_path[chr(96 + index)]
    paths.append(
        {
            'name': path_name,
            'interface': path_name,
            'transport_bind': f'10.91.{index}.12:{61000 + index}',
            'transport_remote': f'10.91.{index}.11:{61100 + index}',
            'scheduler': {
                'mtu': ${PATH_MTU},
                'tx_capacity_bps': capacity_bps,
                'rx_capacity_bps': capacity_bps,
                'reorder_hold_us': ${REORDER_HOLD_US},
            },
        }
    )

security_mode = '${SECURITY_MODE}'
security = {'mode': 'none'}
if security_mode == 'authenticated':
    security = {
        'mode': 'authenticated',
        'local_receiver_index': 2501,
        'remote_receiver_index': 2601,
        'send_key': key(0x81),
        'receive_key': key(0x82),
    }

cfg = {
    'schema_version': 1,
    'node': 'onehop-node-b-source',
    'role': 'client',
    'peer': 'onehop-node-a-sink',
    'scheduler': {'mode': '${SCHEDULER_MODE}', 'traffic_bias': '${SCHEDULER_TRAFFIC_BIAS}'},
    'paths': paths,
    'services': [
        {
            'name': 'udp-speed',
            'listen': '127.0.0.1:55180',
            'target': '127.0.0.1:19092',
            'return_mode': 'learned-single-source',
            'scheduler_flowlet_idle_us': ${FLOWLET_IDLE_US},
            'scheduler_flowlet_max_hold_us': ${FLOWLET_MAX_HOLD_US},
            'scheduler_path_run_datagrams': ${PATH_RUN_DATAGRAMS},
        }
    ],
    'security': security,
}
Path('/tmp/gl-onehop-node-b.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"
}

compile_udp_tool() {
  remote_a "cd /home/gatherlink/src/gatherlink && rustc --edition 2021 -O tools/udp_pressure.rs -o /tmp/gatherlink-udp-pressure"
  remote_b "cd /home/gatherlink/src/gatherlink && rustc --edition 2021 -O tools/udp_pressure.rs -o /tmp/gatherlink-udp-pressure"
}

start_services() {
  local scheduler_reapply_arg=""
  if [[ -n "${SCHEDULER_REAPPLY_INTERVAL}" ]]; then
    scheduler_reapply_arg=" --scheduler-reapply-interval ${SCHEDULER_REAPPLY_INTERVAL}"
  fi
  remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/gl-onehop-node-a.json"
  remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/gl-onehop-node-b.json"
  remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/gl-onehop-node-a.json --name gl-onehop.vm.node-a --batch-size ${CORE_BATCH_SIZE}${scheduler_reapply_arg} --diagnostics-jsonl /tmp/gl-onehop-node-a.jsonl"
  sleep 1
  remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/gl-onehop-node-b.json --name gl-onehop.vm.node-b --batch-size ${CORE_BATCH_SIZE}${scheduler_reapply_arg} --diagnostics-jsonl /tmp/gl-onehop-node-b.jsonl"
  sleep 3
}

start_sink() {
  remote_a "rm -f /tmp/gl-onehop-sink.out /tmp/gl-onehop-sink.err /tmp/gl-onehop-sink.progress /tmp/gl-onehop-sink.pid
nohup /tmp/gatherlink-udp-pressure sink --bind 127.0.0.1:19091 --duration $((DURATION + 10)) --idle-after-first 2 --out /tmp/gl-onehop-sink.progress >/tmp/gl-onehop-sink.out 2>/tmp/gl-onehop-sink.err < /dev/null & echo \$! >/tmp/gl-onehop-sink.pid
sleep 0.3
if ! kill -0 \$(cat /tmp/gl-onehop-sink.pid) 2>/dev/null; then
  cat /tmp/gl-onehop-sink.err >&2
  exit 1
fi"
}

start_competing_traffic() {
  [[ -n "${COMPETING_RATE}" ]] || return 0
  local index path path_name port_number client_target competing_mbit
  competing_mbit="${COMPETING_RATE%M}"
  competing_mbit="${competing_mbit%m}"
  for path in ${ACTIVE_PATH_LETTERS}; do
    index="$(path_letter_index "${path}")"
    path_name="path-${path}"
    port_number=$((54000 + index))
    client_target="10.91.${index}.11"
    remote_a "rm -f /tmp/gl-onehop-compete-${path_name}.sink.json /tmp/gl-onehop-compete-${path_name}.sink.err /tmp/gl-onehop-compete-${path_name}.sink.pid; nohup /tmp/gatherlink-udp-pressure sink --bind 0.0.0.0:${port_number} --duration $((DURATION + 5)) --idle-after-first 2 --out /tmp/gl-onehop-compete-${path_name}.sink.progress >/tmp/gl-onehop-compete-${path_name}.sink.json 2>/tmp/gl-onehop-compete-${path_name}.sink.err < /dev/null & echo \$! >/tmp/gl-onehop-compete-${path_name}.sink.pid"
    remote_b "rm -f /tmp/gl-onehop-compete-${path_name}.json /tmp/gl-onehop-compete-${path_name}.stderr; setsid -f sh -c '/tmp/gatherlink-udp-pressure send --target ${client_target}:${port_number} --duration ${DURATION} --payload-size ${COMPETING_LENGTH} --target-mbit ${competing_mbit} >/tmp/gl-onehop-compete-${path_name}.json 2>/tmp/gl-onehop-compete-${path_name}.stderr' >/dev/null 2>&1"
  done
}

run_generator() {
  local target_arg=()
  if [[ -n "${TARGET_MBIT}" ]]; then
    target_arg=(--target-mbit "${TARGET_MBIT}")
  fi
  remote_b "/tmp/gatherlink-udp-pressure send --target 127.0.0.1:55180 --duration ${DURATION} --payload-size ${PAYLOAD_SIZE} ${target_arg[*]}" >"${OUT_DIR}/generator.json"
}

fetch_results() {
  sleep 3
  fetch_probe "${PORT_A}" /tmp/gl-onehop-sink.out "${OUT_DIR}/sink.json"
  fetch_probe "${PORT_A}" /tmp/gl-onehop-sink.progress "${OUT_DIR}/sink-progress.json"
  remote_a "cat /tmp/gl-onehop-sink.err 2>/dev/null || true" >"${OUT_DIR}/sink.err" || true
  remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status gl-onehop.vm.node-b" >"${OUT_DIR}/status-b.json" || true
  remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status gl-onehop.vm.node-a" >"${OUT_DIR}/status-a.json" || true
  if [[ -n "${COMPETING_RATE}" ]]; then
    local index path path_name
    for path in ${ACTIVE_PATH_LETTERS}; do
      index="$(path_letter_index "${path}")"
      path_name="path-${path}"
      fetch_probe "${PORT_B}" "/tmp/gl-onehop-compete-${path_name}.json" "${OUT_DIR}/compete-${path_name}.json"
      remote_b "cat /tmp/gl-onehop-compete-${path_name}.stderr 2>/dev/null || true" >"${OUT_DIR}/compete-${path_name}.stderr" || true
      fetch_probe "${PORT_A}" "/tmp/gl-onehop-compete-${path_name}.sink.json" "${OUT_DIR}/compete-${path_name}.sink.json"
      remote_a "cat /tmp/gl-onehop-compete-${path_name}.sink.err 2>/dev/null || true" >"${OUT_DIR}/compete-${path_name}.sink.err" || true
    done
  fi
}

path_letter_index() {
  case "$1" in
    a) printf '1' ;;
    b) printf '2' ;;
    c) printf '3' ;;
    d) printf '4' ;;
    e) printf '5' ;;
    *) echo "unsupported active path: $1" >&2; return 2 ;;
  esac
}

record "# Gatherlink Raw UDP One-Hop Speed"
record ""
record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
record "- duration_seconds: ${DURATION}"
record "- payload_size: ${PAYLOAD_SIZE}"
record "- target_mbit: ${TARGET_MBIT:-unlimited}"
record "- link_mtu: ${LINK_MTU}"
record "- path_mtu: ${PATH_MTU}"
record "- core_batch_size: ${CORE_BATCH_SIZE}"
record "- scheduler_reapply_interval: ${SCHEDULER_REAPPLY_INTERVAL:-disabled}"
record "- security_mode: ${SECURITY_MODE}"
record "- active_paths: ${ACTIVE_PATHS}"
record "- scheduler_mode: ${SCHEDULER_MODE}"
record "- scheduler_traffic_bias: ${SCHEDULER_TRAFFIC_BIAS}"
record "- path_capacity_mbits: ${PATH_CAPACITY_MBITS:-default-5000}"
record "- flowlet_idle_us: ${FLOWLET_IDLE_US}"
record "- flowlet_max_hold_us: ${FLOWLET_MAX_HOLD_US}"
record "- path_run_datagrams: ${PATH_RUN_DATAGRAMS}"
record "- reorder_hold_us: ${REORDER_HOLD_US}"
record "- shape_profile: ${SHAPE_PROFILE}"
record "- competing_rate: ${COMPETING_RATE:-disabled}"
record "- competing_length: ${COMPETING_LENGTH}"
record "- setup_only: ${SETUP_ONLY}"
record "- lab_kernel_tuning: ${APPLY_KERNEL_TUNING}"
record "- out: ${OUT_DIR}"
record ""

if [[ "${APPLY_KERNEL_TUNING}" -eq 1 ]]; then
  record "Applying lab UDP socket-buffer tuning to VM A and VM B."
  apply_kernel_tuning
fi
cleanup
"${SCRIPT_DIR}/apply_path_shape_profile.sh" \
  --ip "${IP}" \
  --ports "${PORT_A},${PORT_B}" \
  --active-paths "${ACTIVE_PATHS}" \
  --profile "${SHAPE_PROFILE}" \
  --link-mtu "${LINK_MTU}"
compile_udp_tool
write_configs
start_services
if [[ "${SETUP_ONLY}" -eq 1 ]]; then
  record "Core services were started and left running for external traffic."
  printf '\nGatherlink raw UDP one-hop setup complete.\nReport: %s\n' "${REPORT}"
  exit 0
fi
start_sink
start_competing_traffic
run_generator
fetch_results

python3 - "${OUT_DIR}/generator.json" "${OUT_DIR}/sink.json" "${OUT_DIR}/sink-progress.json" "${REPORT_JSON}" "${OUT_DIR}" <<'PY' | tee -a "${REPORT}"
from __future__ import annotations

import json
import sys
from pathlib import Path

generator = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sink_path = Path(sys.argv[2])
progress_path = Path(sys.argv[3])
report_json = Path(sys.argv[4])
out_dir = Path(sys.argv[5])
sink_text = sink_path.read_text(encoding="utf-8").strip()
if not sink_text:
    sink_text = progress_path.read_text(encoding="utf-8").strip()
sink = json.loads(sink_text) if sink_text else {}
print("## Results")
print(f"generator: {generator['bits_per_second'] / 1_000_000:.2f} Mbit/s packets={generator['packets']} bytes={generator['bytes']}")
results = [
    {
        "name": "generator",
        "bits_per_second": float(generator["bits_per_second"]),
        "mbit_per_second": float(generator["bits_per_second"]) / 1_000_000,
        "packets": int(generator["packets"]),
        "bytes": int(generator["bytes"]),
    }
]
if sink:
    print(f"sink: {sink['bits_per_second'] / 1_000_000:.2f} Mbit/s packets={sink['packets']} bytes={sink['bytes']}")
    dropped = int(generator["packets"]) - int(sink["packets"])
    print(f"application_packet_delta: {dropped}")
    results.append(
        {
            "name": "sink",
            "bits_per_second": float(sink["bits_per_second"]),
            "mbit_per_second": float(sink["bits_per_second"]) / 1_000_000,
            "packets": int(sink["packets"]),
            "bytes": int(sink["bytes"]),
            "application_packet_delta": dropped,
        }
    )
else:
    print("sink: no result")
for compete in sorted(out_dir.glob("compete-path-*.json")):
    if compete.name.endswith(".sink.json"):
        continue
    try:
        data = json.loads(compete.read_text(encoding="utf-8"))
        sink_path = compete.with_name(f"{compete.stem}.sink.json")
        sink_data = json.loads(sink_path.read_text(encoding="utf-8")) if sink_path.exists() else {}
        bps = float(data.get("bits_per_second", 0))
        sink_bps = float(sink_data.get("bits_per_second", 0)) if sink_data else 0.0
        sent_packets = int(data.get("packets", 0))
        sink_packets = int(sink_data.get("packets", 0)) if sink_data else 0
        packet_delta = sent_packets - sink_packets
        print(f"{compete.stem}: send={bps / 1_000_000:.2f} Mbit/s sink={sink_bps / 1_000_000:.2f} Mbit/s delta={packet_delta}")
        results.append(
            {
                "name": compete.stem,
                "bits_per_second": bps,
                "mbit_per_second": bps / 1_000_000,
                "sink_bits_per_second": sink_bps,
                "sink_mbit_per_second": sink_bps / 1_000_000,
                "application_packet_delta": packet_delta,
            }
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        print(f"{compete.stem}: no result")
report_json.write_text(json.dumps({"results": results}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

record ""
record "Report: ${REPORT}"
if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
  record "Services were left up for inspection."
fi
printf '\nGatherlink raw UDP one-hop speed complete.\nReport: %s\n' "${REPORT}"
