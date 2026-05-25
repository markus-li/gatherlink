#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/perf_common.sh"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
PORT_C="2203"
DURATION=20
PAYLOAD_SIZE=1100
TARGET_MBIT=""
PATH_MTU=1472
CORE_BATCH_SIZE=512
PATH_CAPACITY_MBIT=5000
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-routing-speed/$(date -u +%Y%m%dT%H%M%SZ)-gatherlink-relay-udp"
KEEP_RUNNING=0
SETUP_ONLY=0
APPLY_KERNEL_TUNING=1
ACTIVE_PATHS="a,b,c"
SCHEDULER_MODE="round_robin"
FLOWLET_IDLE_US=0
FLOWLET_MAX_HOLD_US=0
PATH_RUN_DATAGRAMS=0
REORDER_HOLD_US=2000

usage() {
  cat <<'USAGE'
Usage: run_relay_udp_speed.sh [options]

Runs raw application UDP through the three-VM untrusted Gatherlink relay path:

  UDP generator on VM B -> Gatherlink core on B -> untrusted relay VM C ->
  relay exits on VM A -> Gatherlink core on VM A -> UDP sink on VM A

This is intentionally not WireGuard-over-Gatherlink. It measures the routed
Gatherlink relay path with ordinary UDP payloads.

Options:
  --ip IP              Management IP used with the WSL portproxy setup. Default 172.22.0.1.
  --port-a PORT        SSH port for VM A. Default 2201.
  --port-b PORT        SSH port for VM B. Default 2202.
  --port-c PORT        SSH port for VM C. Default 2203.
  --duration SECONDS   UDP send duration. Default 20.
  --payload-size BYTES UDP payload size. Default 1100.
  --target-mbit MBIT   Pace the UDP generator to this decimal Mbit/s. Default unlimited.
  --path-mtu BYTES      Gatherlink path MTU in generated configs. Default 1472.
  --core-batch-size N   Core runner batch size passed to gatherlink run start. Default 512.
  --path-capacity-mbit MBIT
                        Static per-path scheduler capacity hint. Default 5000.
  --active-paths LIST   Comma-separated path letters to configure. Default a,b,c; supports a,b,c,d,e.
  --scheduler-mode MODE  Python-selected scheduler mode compiled for Rust. Default round_robin.
  --flowlet-idle-us N   Stick one service/source to a path until idle for N us. Default 0.
  --flowlet-max-hold-us N
                        Maximum continuous service/source path hold in us. Default 0.
  --path-run-datagrams N
                        Maximum hot-burst datagrams per path before rescheduling. Default 0.
  --reorder-hold-us N   Receiver hold time for path reordering in us. Default 2000.
  --out DIR            Report directory.
  --keep-running       Leave Gatherlink relay/core services running for inspection.
  --setup-only         Start relay/core services but do not run the temporary UDP sink/generator.
  --skip-kernel-tuning Do not apply the lab UDP socket-buffer sysctls before the run.
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
    --path-run-datagrams) PATH_RUN_DATAGRAMS="$2"; shift 2 ;;
    --reorder-hold-us) REORDER_HOLD_US="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --setup-only) SETUP_ONLY=1; KEEP_RUNNING=1; shift ;;
    --skip-kernel-tuning) APPLY_KERNEL_TUNING=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "${OUT_DIR}"
REPORT="${OUT_DIR}/report.md"
: >"${REPORT}"

ACTIVE_PATH_LETTERS="${ACTIVE_PATHS//,/ }"
ACTIVE_PATH_INDICES_PY="$(perf_path_indexes "${ACTIVE_PATHS}" | tr ' ' ',' | sed 's/,/, /g')"

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
    if [ -f /tmp/relayudp-sink.pid ]; then
      kill "$(cat /tmp/relayudp-sink.pid)" 2>/dev/null || true
    fi
    sudo ip link del wg-gr-a 2>/dev/null || true
    sudo ip link del wg-gr-b 2>/dev/null || true
    rm -f /tmp/relayudp-sink.out /tmp/relayudp-sink.err /tmp/relayudp-sink.progress /tmp/relayudp-sink.pid
    for path in path-a path-b path-c path-d path-e; do
      sudo tc qdisc del dev "${path}" root 2>/dev/null || true
      sudo ip link set "${path}" up 2>/dev/null || true
    done
  ' >/dev/null 2>&1 || true
}

cleanup() {
  cleanup_node "${PORT_A}"
  cleanup_node "${PORT_B}"
  cleanup_node "${PORT_C}"
}

apply_kernel_tuning() {
  local port
  for port in "${PORT_A}" "${PORT_B}" "${PORT_C}"; do
    remote "${port}" '
      sudo sysctl -w \
        net.core.rmem_max=2147483647 \
        net.core.wmem_max=2147483647 \
        net.core.rmem_default=33554432 \
        net.core.wmem_default=33554432 >/dev/null
    '
  done
}

if [[ "${KEEP_RUNNING}" -eq 0 ]]; then
  trap cleanup EXIT
fi

write_configs() {
  remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

paths = []
path_capacity_bps = int(float('${PATH_CAPACITY_MBIT}') * 1_000_000)
reorder_hold = '${REORDER_HOLD_US}'
for index in [${ACTIVE_PATH_INDICES_PY}]:
    path_name = f'path-{chr(96 + index)}'
    scheduler = {
        'mtu': ${PATH_MTU},
        'tx_capacity_bps': path_capacity_bps,
        'rx_capacity_bps': path_capacity_bps,
        'latency_us': 10_000 + index * 500,
    }
    if reorder_hold not in ('', 'auto'):
        scheduler['reorder_hold_us'] = int(reorder_hold)
    paths.append(
        {
            'name': path_name,
            'interface': path_name,
            'transport_bind': f'10.91.{index}.11:{61100 + index}',
            'transport_remote': f'10.91.{index}.13:{62100 + index}',
            'relay': {'relay_receiver_index': 7200 + index, 'send_key': key(0x50 + index)},
            'scheduler': scheduler,
        }
    )
cfg = {
    'schema_version': 1,
    'node': 'relayudp-node-a-final',
    'role': 'server',
    'peer': 'relayudp-node-b-source',
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
    'scheduler': {'mode': '${SCHEDULER_MODE}'},
    'security': {
        'mode': 'authenticated',
        'local_receiver_index': 2601,
        'remote_receiver_index': 2501,
        'send_key': key(0x82),
        'receive_key': key(0x81),
    },
}
Path('/tmp/relayudp-node-a.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"

  remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

paths = []
path_capacity_bps = int(float('${PATH_CAPACITY_MBIT}') * 1_000_000)
reorder_hold = '${REORDER_HOLD_US}'
for index in [${ACTIVE_PATH_INDICES_PY}]:
    path_name = f'path-{chr(96 + index)}'
    scheduler = {
        'mtu': ${PATH_MTU},
        'tx_capacity_bps': path_capacity_bps,
        'rx_capacity_bps': path_capacity_bps,
        'latency_us': 10_000 + index * 500,
    }
    if reorder_hold not in ('', 'auto'):
        scheduler['reorder_hold_us'] = int(reorder_hold)
    paths.append(
        {
            'name': path_name,
            'interface': path_name,
            'transport_bind': f'10.91.{index}.12:{61000 + index}',
            'transport_remote': f'10.91.{index}.13:{62000 + index}',
            'relay': {'relay_receiver_index': 7100 + index, 'send_key': key(0x40 + index)},
            'scheduler': scheduler,
        }
    )
cfg = {
    'schema_version': 1,
    'node': 'relayudp-node-b-source',
    'role': 'client',
    'peer': 'relayudp-node-a-final',
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
    'scheduler': {'mode': '${SCHEDULER_MODE}'},
    'security': {
        'mode': 'authenticated',
        'local_receiver_index': 2501,
        'remote_receiver_index': 2601,
        'send_key': key(0x81),
        'receive_key': key(0x82),
    },
}
Path('/tmp/relayudp-node-b.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"

  remote_c "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

for index in [${ACTIVE_PATH_INDICES_PY}]:
    path_name = f'path-{chr(96 + index)}'
    ba_cfg = {
        'schema_version': 1,
        'name': f'ba-relay-{path_name}',
        'listen': f'10.91.{index}.13:{62000 + index}',
        'executor': {
            'relay_receiver_index': 7100 + index,
            'next_hop_transport': 'udp',
            'next_hop_address': f'10.91.{index}.11:{61100 + index}',
            'next_hop_receiver_index': 0,
            'direction': 'upstream_to_downstream',
            'topology_generation': 1,
            'expires_at_unix_us': 4102444800000000,
            'max_packet_size': 4096,
        },
        'keys': {'send_key': key(0), 'receive_key': key(0x40 + index)},
    }
    ab_cfg = {
        'schema_version': 1,
        'name': f'ab-relay-{path_name}',
        'listen': f'10.91.{index}.13:{62100 + index}',
        'executor': {
            'relay_receiver_index': 7200 + index,
            'next_hop_transport': 'udp',
            'next_hop_address': f'10.91.{index}.12:{61000 + index}',
            'next_hop_receiver_index': 0,
            'direction': 'downstream_to_upstream',
            'topology_generation': 1,
            'expires_at_unix_us': 4102444800000000,
            'max_packet_size': 4096,
        },
        'keys': {'send_key': key(0), 'receive_key': key(0x50 + index)},
    }
    Path(f'/tmp/relayudp-c-ba-{path_name}.json').write_text(json.dumps(ba_cfg, indent=2, sort_keys=True))
    Path(f'/tmp/relayudp-c-ab-{path_name}.json').write_text(json.dumps(ab_cfg, indent=2, sort_keys=True))
PY"
}

start_services() {
  remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/relayudp-node-a.json"
  remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/relayudp-node-b.json"
  for path in ${ACTIVE_PATH_LETTERS}; do
    remote_c "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run relay-start /tmp/relayudp-c-ba-path-${path}.json --name relayudp.c.relay.ba.path-${path} --diagnostics-jsonl /tmp/relayudp-c-ba-path-${path}.jsonl"
    remote_c "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run relay-start /tmp/relayudp-c-ab-path-${path}.json --name relayudp.c.relay.ab.path-${path} --diagnostics-jsonl /tmp/relayudp-c-ab-path-${path}.jsonl"
  done
  remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/relayudp-node-a.json --name relayudp.vm.node-a --batch-size ${CORE_BATCH_SIZE} --diagnostics-jsonl /tmp/relayudp-node-a.jsonl"
  sleep 1
  remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/relayudp-node-b.json --name relayudp.vm.node-b --batch-size ${CORE_BATCH_SIZE} --diagnostics-jsonl /tmp/relayudp-node-b.jsonl"
  sleep 3
}

start_sink() {
  remote_a "cd /home/gatherlink/src/gatherlink && cat >/tmp/relayudp-sink.py <<'PY'
import json
import socket
import time

bind = ('127.0.0.1', 19091)
max_seconds = float(${DURATION}) + 10.0
idle_after_first = 2.0
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(bind)
sock.settimeout(0.5)
started = time.monotonic()
first = None
last = None
last_progress = started
packets = 0
bytes_seen = 0
progress_path = '/tmp/relayudp-sink.progress'

def snapshot(now):
    elapsed = max((last or now) - (first or started), 0.000001)
    return {
        'packets': packets,
        'bytes': bytes_seen,
        'elapsed_seconds': elapsed,
        'bits_per_second': bytes_seen * 8 / elapsed,
        'complete': False,
    }

def write_progress(now):
    with open(progress_path, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(snapshot(now), sort_keys=True) + '\n')

write_progress(started)
while time.monotonic() - started < max_seconds:
    try:
        data, _ = sock.recvfrom(65535)
    except TimeoutError:
        now = time.monotonic()
        if now - last_progress >= 1.0:
            write_progress(now)
            last_progress = now
        if first is not None and last is not None and time.monotonic() - last >= idle_after_first:
            break
        continue
    now = time.monotonic()
    if first is None:
        first = now
    last = now
    packets += 1
    bytes_seen += len(data)
    if now - last_progress >= 1.0:
        write_progress(now)
        last_progress = now
elapsed = max((last or time.monotonic()) - (first or started), 0.000001)
final = {'packets': packets, 'bytes': bytes_seen, 'elapsed_seconds': elapsed, 'bits_per_second': bytes_seen * 8 / elapsed, 'complete': True}
with open(progress_path, 'w', encoding='utf-8') as handle:
    handle.write(json.dumps(final, sort_keys=True) + '\n')
print(json.dumps(final, sort_keys=True))
PY
rm -f /tmp/relayudp-sink.out /tmp/relayudp-sink.err /tmp/relayudp-sink.progress /tmp/relayudp-sink.pid
nohup .venv/bin/python /tmp/relayudp-sink.py >/tmp/relayudp-sink.out 2>/tmp/relayudp-sink.err < /dev/null & echo \$! >/tmp/relayudp-sink.pid
sleep 0.3
if ! kill -0 \$(cat /tmp/relayudp-sink.pid) 2>/dev/null; then
  cat /tmp/relayudp-sink.err >&2
  exit 1
fi"
}

run_generator() {
  remote_b "cd /home/gatherlink/src/gatherlink && cat >/tmp/relayudp-send.py <<'PY'
import json
import socket
import time

target = ('127.0.0.1', 55180)
duration = float(${DURATION})
payload_size = int(${PAYLOAD_SIZE})
target_mbit = '${TARGET_MBIT}'
target_bps = float(target_mbit) * 1_000_000 if target_mbit else None
payload = b'u' * payload_size
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
started = time.monotonic()
next_send = started
packets = 0
bytes_sent = 0
while time.monotonic() - started < duration:
    bytes_sent += sock.sendto(payload, target)
    packets += 1
    if target_bps:
        next_send += (payload_size * 8) / target_bps
        sleep_for = next_send - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
elapsed = time.monotonic() - started
print(json.dumps({'packets': packets, 'bytes': bytes_sent, 'elapsed_seconds': elapsed, 'bits_per_second': bytes_sent * 8 / elapsed}, sort_keys=True))
PY
.venv/bin/python /tmp/relayudp-send.py" >"${OUT_DIR}/generator.json"
}

start_perf_probes() {
  local probe_duration
  probe_duration=$((DURATION + 2))
  for node in a b c; do
    local port
    case "${node}" in
      a) port="${PORT_A}" ;;
      b) port="${PORT_B}" ;;
      c) port="${PORT_C}" ;;
    esac
    remote "${port}" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/relayudp-perf-${node}.json && setsid -f .venv/bin/python tools/hyperv/vm_perf_probe.py --duration ${probe_duration} --interval 0.5 --out /tmp/relayudp-perf-${node}.json --match gatherlink --match relayudp --match python --match iperf3 --netdev path-a --netdev path-b --netdev path-c >/tmp/relayudp-perf-${node}.log 2>&1 < /dev/null"
  done
}

fetch_perf_probes() {
  for _ in 1 2 3 4 5; do
    if remote_a "test -s /tmp/relayudp-perf-a.json" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  remote_a "cat /tmp/relayudp-perf-a.json 2>/dev/null || true" >"${OUT_DIR}/perf-node-a.json" || true
  remote_b "cat /tmp/relayudp-perf-b.json 2>/dev/null || true" >"${OUT_DIR}/perf-node-b.json" || true
  remote_c "cat /tmp/relayudp-perf-c.json 2>/dev/null || true" >"${OUT_DIR}/perf-node-c.json" || true
}

fetch_results() {
  sleep 3
  remote_a "cat /tmp/relayudp-sink.out" >"${OUT_DIR}/sink.json" || true
  remote_a "cat /tmp/relayudp-sink.progress 2>/dev/null || true" >"${OUT_DIR}/sink-progress.json" || true
  remote_a "cat /tmp/relayudp-sink.err 2>/dev/null || true" >"${OUT_DIR}/sink.err" || true
  remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status relayudp.vm.node-b" >"${OUT_DIR}/status-b.json" || true
  remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status relayudp.vm.node-a" >"${OUT_DIR}/status-a.json" || true
  remote_c "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" >"${OUT_DIR}/services-c.txt" || true
  fetch_perf_probes
}

record "# Gatherlink Raw UDP Relay Speed"
record ""
record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
record "- duration_seconds: ${DURATION}"
record "- payload_size: ${PAYLOAD_SIZE}"
record "- target_mbit: ${TARGET_MBIT:-unlimited}"
record "- path_mtu: ${PATH_MTU}"
record "- core_batch_size: ${CORE_BATCH_SIZE}"
record "- path_capacity_mbit: ${PATH_CAPACITY_MBIT}"
record "- active_paths: ${ACTIVE_PATHS}"
record "- scheduler_mode: ${SCHEDULER_MODE}"
record "- flowlet_idle_us: ${FLOWLET_IDLE_US}"
record "- flowlet_max_hold_us: ${FLOWLET_MAX_HOLD_US}"
record "- path_run_datagrams: ${PATH_RUN_DATAGRAMS}"
record "- reorder_hold_us: ${REORDER_HOLD_US}"
record "- setup_only: ${SETUP_ONLY}"
record "- lab_kernel_tuning: ${APPLY_KERNEL_TUNING}"
record "- out: ${OUT_DIR}"
record ""

if [[ "${APPLY_KERNEL_TUNING}" -eq 1 ]]; then
  record "Applying lab UDP socket-buffer tuning to VM A, VM B, and relay VM C."
  apply_kernel_tuning
fi
cleanup
write_configs
start_services
if [[ "${SETUP_ONLY}" -eq 1 ]]; then
  record "Relay/core services were started and left running for an external application such as WireGuard."
  printf '\nGatherlink raw UDP relay setup complete.\nReport: %s\n' "${REPORT}"
  exit 0
fi
start_sink
start_perf_probes
run_generator
fetch_results

python3 - "${OUT_DIR}/generator.json" "${OUT_DIR}/sink.json" "${OUT_DIR}" <<'PY' | tee -a "${REPORT}"
from __future__ import annotations

import json
import sys
from pathlib import Path

generator = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sink_path = Path(sys.argv[2])
out_dir = Path(sys.argv[3])
report_json = sink_path.with_name("report.json")
sink_text = sink_path.read_text(encoding="utf-8").strip()
if not sink_text:
    progress_path = sink_path.with_name("sink-progress.json")
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
perf = {}
for node in ["a", "b", "c"]:
    path = out_dir / f"perf-node-{node}.json"
    if path.read_text(encoding="utf-8").strip():
        perf[node] = json.loads(path.read_text(encoding="utf-8"))
if perf:
    print("## Perf Probe")
    for node, data in perf.items():
        udp = data.get("udp_delta", {})
        print(
            f"node-{node}: cpu_busy={data.get('cpu_busy_percent_all_cores', 0):.1f}% "
            f"udp_in_errors={udp.get('InErrors', 0)} udp_rcvbuf_errors={udp.get('RcvbufErrors', 0)} "
            f"udp_sndbuf_errors={udp.get('SndbufErrors', 0)}"
        )
report_json.write_text(json.dumps({"results": results, "perf": perf}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

record ""
record "Report: ${REPORT}"
if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
  record "Relay services were left up for inspection."
fi
printf '\nGatherlink raw UDP relay speed complete.\nReport: %s\n' "${REPORT}"
