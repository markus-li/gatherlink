#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/perf_common.sh"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
DURATION=15
PARALLEL=12
UDP_RATE="1000M"
UDP_LENGTH=1300
LINK_MTU=1500
WG_MTU=1380
PATH_MTU=1472
CORE_BATCH_SIZE=512
SCHEDULER_REAPPLY_INTERVAL=1
PATH_PACING_MBITS=""
SECURITY_MODE="authenticated"
ACTIVE_PATHS="a,b,c"
STABLE_PATHS=""
FAST_PATHS=""
FAST_PATH_HEADROOM="1.25"
SHAPE_PROFILE="clean"
SCHEDULER_MODE="coordinated_adaptive"
SCHEDULER_TRAFFIC_BIAS="udp"
PATH_CAPACITY_MBITS="a:5000,b:5000,c:5000"
STABLE_PATH_POLICY="single_best_path"
FAST_PATH_POLICY="weighted_round_robin"
STABLE_POLL_BATCH_PACKETS=128
FLOWLET_IDLE_US=50000
FLOWLET_MAX_HOLD_US=60000000
FAST_POLL_BATCH_PACKETS=0
FAST_PATH_RUN_DATAGRAMS=0
REORDER_HOLD_US=2000
RUN_MIXED=0
OUTCOME_TCP_MIN_MBIT=""
OUTCOME_TCP_MAX_RETRANS=1000
OUTCOME_UDP_MAX_LOSS_PERCENT="0.10"
SEED_STABLE_OUTCOME=0
OUT_DIR="$(perf_repo_root)/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-dual-wireguard-gatherlink-speed"

usage() {
  cat <<'USAGE'
Usage: run_dual_wireguard_gatherlink_speed.sh [options]

Starts two WireGuard-over-Gatherlink services:

  stable/default profile -> TCP/stability-oriented WireGuard interface
  fast/UDP profile       -> UDP/throughput-oriented WireGuard interface

The script uses the WireGuard helper plan and traffic-split dry-run commands,
then benchmarks TCP over the stable interface and UDP over the fast interface.
It does not apply host firewall policy by default.

Options mirror the normal one-hop WireGuard runner:
  --ip IP
  --port-a PORT
  --port-b PORT
  --duration SECONDS
  --parallel N
  --udp-rate RATE
  --udp-length BYTES
  --link-mtu BYTES
  --wg-mtu BYTES
  --path-mtu BYTES
  --core-batch-size N
  --scheduler-reapply-interval SECONDS
  --path-pacing-mbits SPEC
  --security-mode MODE
  --active-paths LIST
  --stable-paths LIST
  --fast-paths LIST
  --fast-path-headroom MULTIPLIER
  --shape-profile NAME
  --scheduler-mode MODE
  --scheduler-traffic-bias BIAS
  --path-capacity-mbits SPEC
  --stable-path-policy POLICY
  --fast-path-policy POLICY
  --stable-poll-batch-packets N
  --flowlet-idle-us N
  --flowlet-max-hold-us N
  --fast-poll-batch-packets N
  --fast-path-run-datagrams N
  --reorder-hold-us N
  --outcome-tcp-min-mbit MBIT
  --outcome-tcp-max-retrans N
  --outcome-udp-max-loss-percent PERCENT
  --seed-stable-outcome Seed a degraded stable-service outcome on both nodes before benchmarking.
  --out DIR
  --keep-running
  --skip-kernel-tuning
  --mixed              Run stable TCP and fast UDP concurrently.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --port-a) PORT_A="$2"; shift 2 ;;
    --port-b) PORT_B="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --udp-rate) UDP_RATE="$2"; shift 2 ;;
    --udp-length) UDP_LENGTH="$2"; shift 2 ;;
    --link-mtu) LINK_MTU="$2"; shift 2 ;;
    --wg-mtu) WG_MTU="$2"; shift 2 ;;
    --path-mtu) PATH_MTU="$2"; shift 2 ;;
    --core-batch-size) CORE_BATCH_SIZE="$2"; shift 2 ;;
    --scheduler-reapply-interval) SCHEDULER_REAPPLY_INTERVAL="$2"; shift 2 ;;
    --path-pacing-mbits) PATH_PACING_MBITS="$2"; shift 2 ;;
    --security-mode) SECURITY_MODE="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --stable-paths) STABLE_PATHS="$2"; shift 2 ;;
    --fast-paths) FAST_PATHS="$2"; shift 2 ;;
    --fast-path-headroom) FAST_PATH_HEADROOM="$2"; shift 2 ;;
    --shape-profile) SHAPE_PROFILE="$2"; shift 2 ;;
    --scheduler-mode) SCHEDULER_MODE="$2"; shift 2 ;;
    --scheduler-traffic-bias) SCHEDULER_TRAFFIC_BIAS="$2"; shift 2 ;;
    --path-capacity-mbits) PATH_CAPACITY_MBITS="$2"; shift 2 ;;
    --stable-path-policy) STABLE_PATH_POLICY="$2"; shift 2 ;;
    --fast-path-policy) FAST_PATH_POLICY="$2"; shift 2 ;;
    --stable-poll-batch-packets) STABLE_POLL_BATCH_PACKETS="$2"; shift 2 ;;
    --flowlet-idle-us) FLOWLET_IDLE_US="$2"; shift 2 ;;
    --flowlet-max-hold-us) FLOWLET_MAX_HOLD_US="$2"; shift 2 ;;
    --fast-poll-batch-packets) FAST_POLL_BATCH_PACKETS="$2"; shift 2 ;;
    --fast-path-run-datagrams) FAST_PATH_RUN_DATAGRAMS="$2"; shift 2 ;;
    --reorder-hold-us) REORDER_HOLD_US="$2"; shift 2 ;;
    --outcome-tcp-min-mbit) OUTCOME_TCP_MIN_MBIT="$2"; shift 2 ;;
    --outcome-tcp-max-retrans) OUTCOME_TCP_MAX_RETRANS="$2"; shift 2 ;;
    --outcome-udp-max-loss-percent) OUTCOME_UDP_MAX_LOSS_PERCENT="$2"; shift 2 ;;
    --seed-stable-outcome) SEED_STABLE_OUTCOME=1; shift ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --keep-running) PERF_KEEP_RUNNING=1; shift ;;
    --skip-kernel-tuning) PERF_APPLY_KERNEL_TUNING=0; shift ;;
    --mixed) RUN_MIXED=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

perf_init_defaults
REPORT="${OUT_DIR}/report.md"
LIVE_OUTCOME_SERVICE_RECORD="gl-dual-wg.vm.node-b"
: >"${REPORT}"
if [[ "${PERF_KEEP_RUNNING}" -eq 0 ]]; then
  trap perf_cleanup_all EXIT
fi

ACTIVE_PATH_LETTERS="$(printf '%s' "${ACTIVE_PATHS}" | tr ',' ' ')"
if [[ -z "${STABLE_PATHS}" ]]; then
  STABLE_PATHS="$(
    python3 - "${ACTIVE_PATHS}" "${PATH_CAPACITY_MBITS}" <<'PY'
import sys

active_paths = [item.strip() for item in sys.argv[1].split(",") if item.strip()]
capacities = {}
for item in sys.argv[2].split(","):
    if not item:
        continue
    name, value = item.split(":", 1)
    capacities[name.strip()] = float(value)
best = max(active_paths, key=lambda path: (capacities.get(path, 0), -active_paths.index(path)))
print(best)
PY
  )"
fi
if [[ -z "${FAST_PATHS}" ]]; then
  FAST_PATHS="$(
    python3 - "${ACTIVE_PATHS}" "${STABLE_PATHS}" "${PATH_CAPACITY_MBITS}" "${UDP_RATE}" "${FAST_PATH_HEADROOM}" <<'PY'
import sys

active_paths = [item.strip() for item in sys.argv[1].split(",") if item.strip()]
stable_paths = {item.strip() for item in sys.argv[2].split(",") if item.strip()}
capacities = {}
for item in sys.argv[3].split(","):
    if not item:
        continue
    name, value = item.split(":", 1)
    capacities[name.strip()] = float(value)
rate_text = sys.argv[4].strip()
suffix = rate_text[-1:].lower()
number_text = rate_text[:-1] if suffix.isalpha() else rate_text
target_mbit = float(number_text)
if suffix == "k":
    target_mbit /= 1000
elif suffix == "g":
    target_mbit *= 1000
elif suffix.isalpha() and suffix != "m":
    raise SystemExit(f"unsupported UDP rate suffix: {rate_text}")
headroom = float(sys.argv[5])
if headroom < 1:
    raise SystemExit("--fast-path-headroom must be at least 1")
remaining = [path for path in active_paths if path not in stable_paths] or active_paths
required_mbit = target_mbit * headroom
ordered_remaining = sorted(remaining, key=lambda item: (-capacities.get(item, 0), remaining.index(item)))
selected = []
selected_capacity = 0.0
for path in ordered_remaining:
    selected.append(path)
    selected_capacity += capacities.get(path, 0)
    if selected_capacity >= required_mbit:
        break
# Keep the fast class on the smallest path set that satisfies the configured
# headroom. Adding an unused spare path can create avoidable jitter/reorder in
# the inner WireGuard flow and steal runtime budget from the stable service.
fast_paths = [path for path in remaining if path in set(selected)]
print(",".join(fast_paths))
PY
  )"
fi
ACTIVE_PATH_INDICES_PY="$(
  python3 - "${ACTIVE_PATHS}" <<'PY'
import sys

items = [item.strip() for item in sys.argv[1].split(",") if item.strip()]
print(",".join(str(ord(item) - 96) for item in items))
PY
)"
PATH_CAPACITY_JSON="$(
  python3 - "${PATH_CAPACITY_MBITS}" <<'PY'
import json
import sys

result = {}
for item in sys.argv[1].split(","):
    if not item:
        continue
    name, value = item.split(":", 1)
    result[name.strip()] = int(float(value) * 1_000_000)
print(json.dumps(result))
PY
)"
PATH_PACING_JSON="$(
  python3 - "${PATH_PACING_MBITS}" <<'PY'
import json
import sys

result = {}
for item in sys.argv[1].split(","):
    if not item:
        continue
    name, value = item.split(":", 1)
    result[name.strip()] = int(float(value) * 1_000_000)
print(json.dumps(result))
PY
)"
STABLE_PATHS_JSON="$(
  python3 - "${STABLE_PATHS}" <<'PY'
import json
import sys

print(json.dumps([item.strip() for item in sys.argv[1].split(",") if item.strip()]))
PY
)"
FAST_PATHS_JSON="$(
  python3 - "${FAST_PATHS}" <<'PY'
import json
import sys

print(json.dumps([item.strip() for item in sys.argv[1].split(",") if item.strip()]))
PY
)"

perf_record "# Dual WireGuard Over Gatherlink Speed"
perf_record ""
perf_record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
perf_record "- duration_seconds: ${DURATION}"
perf_record "- tcp_parallel: ${PARALLEL}"
perf_record "- udp_rate: ${UDP_RATE}"
perf_record "- udp_length: ${UDP_LENGTH}"
perf_record "- iperf_tcp_client_args: ${PERF_IPERF_TCP_CLIENT_ARGS:-[none]}"
perf_record "- iperf_tcp_server_args: ${PERF_IPERF_TCP_SERVER_ARGS:-[none]}"
perf_record "- link_mtu: ${LINK_MTU}"
perf_record "- wg_mtu: ${WG_MTU}"
perf_record "- path_mtu: ${PATH_MTU}"
perf_record "- core_batch_size: ${CORE_BATCH_SIZE}"
perf_record "- scheduler_reapply_interval: ${SCHEDULER_REAPPLY_INTERVAL}"
perf_record "- path_pacing_mbits: ${PATH_PACING_MBITS:-[none]}"
perf_record "- security_mode: ${SECURITY_MODE}"
perf_record "- active_paths: ${ACTIVE_PATHS}"
perf_record "- stable_paths: ${STABLE_PATHS}"
perf_record "- fast_paths: ${FAST_PATHS}"
perf_record "- fast_path_headroom: ${FAST_PATH_HEADROOM}"
perf_record "- shape_profile: ${SHAPE_PROFILE}"
perf_record "- scheduler_mode: ${SCHEDULER_MODE}"
perf_record "- scheduler_traffic_bias: ${SCHEDULER_TRAFFIC_BIAS}"
perf_record "- path_capacity_mbits: ${PATH_CAPACITY_MBITS}"
perf_record "- stable_path_policy: ${STABLE_PATH_POLICY}"
perf_record "- fast_path_policy: ${FAST_PATH_POLICY}"
perf_record "- stable_poll_batch_packets: ${STABLE_POLL_BATCH_PACKETS}"
perf_record "- stable_flowlet_idle_us: ${FLOWLET_IDLE_US}"
perf_record "- stable_flowlet_max_hold_us: ${FLOWLET_MAX_HOLD_US}"
perf_record "- fast_poll_batch_packets: ${FAST_POLL_BATCH_PACKETS}"
perf_record "- fast_path_run_datagrams: ${FAST_PATH_RUN_DATAGRAMS}"
perf_record "- outcome_tcp_min_mbit: ${OUTCOME_TCP_MIN_MBIT:-[none]}"
perf_record "- outcome_tcp_max_retrans: ${OUTCOME_TCP_MAX_RETRANS}"
perf_record "- outcome_udp_max_loss_percent: ${OUTCOME_UDP_MAX_LOSS_PERCENT}"
perf_record "- seed_stable_outcome: ${SEED_STABLE_OUTCOME}"
perf_record "- live_service_outcome_ipc: ${LIVE_OUTCOME_SERVICE_RECORD}"
perf_record "- stable_scheduler_allowed_paths: ${STABLE_PATHS}"
perf_record "- fast_scheduler_allowed_paths: ${FAST_PATHS}"
perf_record "- service_scheduler_path_weights: capacity-derived per service"
perf_record "- run_mixed: ${RUN_MIXED}"
perf_record "- output: ${OUT_DIR}"
perf_record ""

perf_cleanup_all
if [[ "${PERF_APPLY_KERNEL_TUNING}" -eq 1 ]]; then
  perf_step "Kernel Tuning"
  perf_apply_kernel_tuning
fi

perf_step "Path Prep"
"${SCRIPT_DIR}/apply_path_shape_profile.sh" \
  --ip "${IP}" \
  --ports "${PORT_A},${PORT_B}" \
  --active-paths "${ACTIVE_PATHS}" \
  --profile "${SHAPE_PROFILE}" \
  --link-mtu "${LINK_MTU}"

perf_step "Write Dual Gatherlink Configs"
perf_remote_a "cd ${PERF_REMOTE_REPO} && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

capacity_bps_by_path = ${PATH_CAPACITY_JSON}
pacing_bps_by_path = ${PATH_PACING_JSON}
paths = []
for index in [${ACTIVE_PATH_INDICES_PY}]:
    path_letter = chr(96 + index)
    path_name = f'path-{path_letter}'
    capacity_bps = capacity_bps_by_path[path_letter]
    scheduler = {
        'mtu': ${PATH_MTU},
        'tx_capacity_bps': capacity_bps,
        'rx_capacity_bps': capacity_bps,
        'reorder_hold_us': ${REORDER_HOLD_US},
    }
    if pacing_bps_by_path.get(path_letter, 0) > 0:
        scheduler['pacing_budget_bps'] = pacing_bps_by_path[path_letter]
    paths.append(
        {
            'name': path_name,
            'interface': path_name,
            'transport_bind': f'10.91.{index}.11:{61100 + index}',
            'transport_remote': f'10.91.{index}.12:{61000 + index}',
            'scheduler': scheduler,
        }
    )

stable_path_letters = ${STABLE_PATHS_JSON}
fast_path_letters = ${FAST_PATHS_JSON}
stable_allowed_paths = [f'path-{path_letter}' for path_letter in stable_path_letters]
fast_allowed_paths = [f'path-{path_letter}' for path_letter in fast_path_letters]
missing = [path_name for path_name in [*stable_allowed_paths, *fast_allowed_paths] if path_name not in {path['name'] for path in paths}]
if missing:
    raise SystemExit(f'allowed service path is not active: {missing}')
stable_path_weights = {path_name: max(1, capacity_bps_by_path[path_name.rsplit('-', 1)[1]] // 1_000_000) for path_name in stable_allowed_paths}
fast_path_weights = {path_name: max(1, capacity_bps_by_path[path_name.rsplit('-', 1)[1]] // 1_000_000) for path_name in fast_allowed_paths}

security = {'mode': 'none'}
if '${SECURITY_MODE}' == 'authenticated':
    security = {
        'mode': 'authenticated',
        'local_receiver_index': 3601,
        'remote_receiver_index': 3501,
        'send_key': key(0x92),
        'receive_key': key(0x91),
    }

cfg = {
    'schema_version': 1,
    'node': 'dual-wg-node-a-sink',
    'role': 'server',
    'peer': 'dual-wg-node-b-source',
    'scheduler': {'mode': '${SCHEDULER_MODE}', 'traffic_bias': '${SCHEDULER_TRAFFIC_BIAS}'},
    'paths': paths,
    'services': [
        {
            'name': 'wireguard-stable',
            'listen': '127.0.0.1:0',
            'target': '127.0.0.1:19091',
            'return_mode': 'fixed',
            'priority': 'high',
            'scheduler_poll_batch_packets': ${STABLE_POLL_BATCH_PACKETS},
            'scheduler_path_policy': '${STABLE_PATH_POLICY}',
            'scheduler_allowed_paths': stable_allowed_paths,
            'scheduler_path_weights': stable_path_weights,
            'scheduler_flowlet_idle_us': ${FLOWLET_IDLE_US},
            'scheduler_flowlet_max_hold_us': ${FLOWLET_MAX_HOLD_US},
        },
        {
            'name': 'wireguard-fast',
            'listen': '127.0.0.2:0',
            'target': '127.0.0.1:19093',
            'return_mode': 'fixed',
            'priority': 'bulk',
            'scheduler_poll_batch_packets': ${FAST_POLL_BATCH_PACKETS},
            'scheduler_path_policy': '${FAST_PATH_POLICY}',
            'scheduler_allowed_paths': fast_allowed_paths,
            'scheduler_path_weights': fast_path_weights,
            'scheduler_path_run_datagrams': ${FAST_PATH_RUN_DATAGRAMS},
        },
    ],
    'security': security,
}
Path('/tmp/gl-dual-node-a.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"

perf_remote_b "cd ${PERF_REMOTE_REPO} && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

capacity_bps_by_path = ${PATH_CAPACITY_JSON}
pacing_bps_by_path = ${PATH_PACING_JSON}
paths = []
for index in [${ACTIVE_PATH_INDICES_PY}]:
    path_letter = chr(96 + index)
    path_name = f'path-{path_letter}'
    capacity_bps = capacity_bps_by_path[path_letter]
    scheduler = {
        'mtu': ${PATH_MTU},
        'tx_capacity_bps': capacity_bps,
        'rx_capacity_bps': capacity_bps,
        'reorder_hold_us': ${REORDER_HOLD_US},
    }
    if pacing_bps_by_path.get(path_letter, 0) > 0:
        scheduler['pacing_budget_bps'] = pacing_bps_by_path[path_letter]
    paths.append(
        {
            'name': path_name,
            'interface': path_name,
            'transport_bind': f'10.91.{index}.12:{61000 + index}',
            'transport_remote': f'10.91.{index}.11:{61100 + index}',
            'scheduler': scheduler,
        }
    )

stable_path_letters = ${STABLE_PATHS_JSON}
fast_path_letters = ${FAST_PATHS_JSON}
stable_allowed_paths = [f'path-{path_letter}' for path_letter in stable_path_letters]
fast_allowed_paths = [f'path-{path_letter}' for path_letter in fast_path_letters]
missing = [path_name for path_name in [*stable_allowed_paths, *fast_allowed_paths] if path_name not in {path['name'] for path in paths}]
if missing:
    raise SystemExit(f'allowed service path is not active: {missing}')
stable_path_weights = {path_name: max(1, capacity_bps_by_path[path_name.rsplit('-', 1)[1]] // 1_000_000) for path_name in stable_allowed_paths}
fast_path_weights = {path_name: max(1, capacity_bps_by_path[path_name.rsplit('-', 1)[1]] // 1_000_000) for path_name in fast_allowed_paths}

security = {'mode': 'none'}
if '${SECURITY_MODE}' == 'authenticated':
    security = {
        'mode': 'authenticated',
        'local_receiver_index': 3501,
        'remote_receiver_index': 3601,
        'send_key': key(0x91),
        'receive_key': key(0x92),
    }

cfg = {
    'schema_version': 1,
    'node': 'dual-wg-node-b-source',
    'role': 'client',
    'peer': 'dual-wg-node-a-sink',
    'scheduler': {'mode': '${SCHEDULER_MODE}', 'traffic_bias': '${SCHEDULER_TRAFFIC_BIAS}'},
    'paths': paths,
    'services': [
        {
            'name': 'wireguard-stable',
            'listen': '127.0.0.1:55180',
            'target': '127.0.0.1:19092',
            'return_mode': 'learned-single-source',
            'priority': 'high',
            'scheduler_poll_batch_packets': ${STABLE_POLL_BATCH_PACKETS},
            'scheduler_path_policy': '${STABLE_PATH_POLICY}',
            'scheduler_allowed_paths': stable_allowed_paths,
            'scheduler_path_weights': stable_path_weights,
            'scheduler_flowlet_idle_us': ${FLOWLET_IDLE_US},
            'scheduler_flowlet_max_hold_us': ${FLOWLET_MAX_HOLD_US},
        },
        {
            'name': 'wireguard-fast',
            'listen': '127.0.0.1:55181',
            'target': '127.0.0.1:19094',
            'return_mode': 'learned-single-source',
            'priority': 'bulk',
            'scheduler_poll_batch_packets': ${FAST_POLL_BATCH_PACKETS},
            'scheduler_path_policy': '${FAST_PATH_POLICY}',
            'scheduler_allowed_paths': fast_allowed_paths,
            'scheduler_path_weights': fast_path_weights,
            'scheduler_path_run_datagrams': ${FAST_PATH_RUN_DATAGRAMS},
        },
    ],
    'helpers': {
        'wireguard': {
            'enabled': True,
            'mode': 'dual_profile',
            'stable_service': 'wireguard-stable',
            'fast_service': 'wireguard-fast',
        }
    },
    'security': security,
}
Path('/tmp/gl-dual-node-b.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"

perf_remote_a "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink config validate /tmp/gl-dual-node-a.json"
perf_remote_b "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink config validate /tmp/gl-dual-node-b.json"

perf_step "Helper Plans"
perf_remote_b "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink helpers wireguard-plan /tmp/gl-dual-node-b.json | tee /tmp/gl-dual-wireguard-plan.txt"
perf_remote_b "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink helpers traffic-split --stable-interface wg-gl-stable-b --fast-interface wg-gl-fast-b | tee /tmp/gl-dual-traffic-split.txt"
perf_remote_b "grep -q 'traffic_class: stable' /tmp/gl-dual-wireguard-plan.txt && grep -q 'traffic_class: fast' /tmp/gl-dual-wireguard-plan.txt && grep -q 'gatherlink_split' /tmp/gl-dual-traffic-split.txt"

perf_step "Start Gatherlink Services"
perf_remote_b "rm -f /tmp/gl-dual-wg-live-outcome.log"
perf_remote_a "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink run start /tmp/gl-dual-node-a.json --name gl-dual-wg.vm.node-a --batch-size ${CORE_BATCH_SIZE} --scheduler-reapply-interval ${SCHEDULER_REAPPLY_INTERVAL} --diagnostics-jsonl /tmp/gl-dual-node-a.jsonl"
sleep 1
perf_remote_b "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink run start /tmp/gl-dual-node-b.json --name gl-dual-wg.vm.node-b --batch-size ${CORE_BATCH_SIZE} --scheduler-reapply-interval ${SCHEDULER_REAPPLY_INTERVAL} --diagnostics-jsonl /tmp/gl-dual-node-b.jsonl"
sleep 3
if [[ "${SEED_STABLE_OUTCOME}" -eq 1 ]]; then
  perf_remote_a "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink services outcome gl-dual-wg.vm.node-a --service wireguard-stable --degraded --reason 'seeded benchmark stable-service protection'"
  perf_remote_b "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink services outcome gl-dual-wg.vm.node-b --service wireguard-stable --degraded --reason 'seeded benchmark stable-service protection'"
fi

perf_step "WireGuard Setup"
perf_remote_a 'if [ ! -s "$HOME/wg-dual.key" ]; then umask 077; wg genkey > "$HOME/wg-dual.key"; fi; wg pubkey < "$HOME/wg-dual.key" > "$HOME/wg-dual.pub"'
perf_remote_b 'if [ ! -s "$HOME/wg-dual.key" ]; then umask 077; wg genkey > "$HOME/wg-dual.key"; fi; wg pubkey < "$HOME/wg-dual.key" > "$HOME/wg-dual.pub"'
WG_A_PUB="$(perf_remote_a 'cat "$HOME/wg-dual.pub"' | tr -d '\r\n')"
WG_B_PUB="$(perf_remote_b 'cat "$HOME/wg-dual.pub"' | tr -d '\r\n')"
perf_remote_a "sudo ip link del wg-gl-stable-a 2>/dev/null || true; sudo ip link add wg-gl-stable-a type wireguard; sudo ip addr add 10.205.0.1/24 dev wg-gl-stable-a; sudo wg set wg-gl-stable-a listen-port 19091 private-key ${PERF_REMOTE_HOME}/wg-dual.key peer '${WG_B_PUB}' allowed-ips 10.205.0.2/32; sudo ip link set wg-gl-stable-a mtu ${WG_MTU} up"
perf_remote_b "sudo ip link del wg-gl-stable-b 2>/dev/null || true; sudo ip link add wg-gl-stable-b type wireguard; sudo ip addr add 10.205.0.2/24 dev wg-gl-stable-b; sudo wg set wg-gl-stable-b listen-port 19092 private-key ${PERF_REMOTE_HOME}/wg-dual.key peer '${WG_A_PUB}' allowed-ips 10.205.0.1/32 endpoint 127.0.0.1:55180 persistent-keepalive 5; sudo ip link set wg-gl-stable-b mtu ${WG_MTU} up"
perf_remote_a "sudo ip link del wg-gl-fast-a 2>/dev/null || true; sudo ip link add wg-gl-fast-a type wireguard; sudo ip addr add 10.206.0.1/24 dev wg-gl-fast-a; sudo wg set wg-gl-fast-a listen-port 19093 private-key ${PERF_REMOTE_HOME}/wg-dual.key peer '${WG_B_PUB}' allowed-ips 10.206.0.2/32; sudo ip link set wg-gl-fast-a mtu ${WG_MTU} up"
perf_remote_b "sudo ip link del wg-gl-fast-b 2>/dev/null || true; sudo ip link add wg-gl-fast-b type wireguard; sudo ip addr add 10.206.0.2/24 dev wg-gl-fast-b; sudo wg set wg-gl-fast-b listen-port 19094 private-key ${PERF_REMOTE_HOME}/wg-dual.key peer '${WG_A_PUB}' allowed-ips 10.206.0.1/32 endpoint 127.0.0.1:55181 persistent-keepalive 5; sudo ip link set wg-gl-fast-b mtu ${WG_MTU} up"
sleep 3
perf_remote_b "ping -c 3 -W 1 10.205.0.1 && ping -c 3 -W 1 10.206.0.1" | tee "${OUT_DIR}/wg-dual-ping.txt"

perf_step "Benchmarks"
probe_duration=$((DURATION * (2 - RUN_MIXED) + 2))
perf_start_node_probe "dualwg-node-a" "${PORT_A}" "${probe_duration}"
perf_start_node_probe "dualwg-node-b" "${PORT_B}" "${probe_duration}"
perf_remote_b "cd ${PERF_REMOTE_REPO} && setsid -f .venv/bin/python tools/hyperv/live_tcp_outcome_probe.py --duration ${probe_duration} --interval 0.5 --service-record ${LIVE_OUTCOME_SERVICE_RECORD} --runtime-service wireguard-stable --target 10.205.0.1 --max-retrans ${OUTCOME_TCP_MAX_RETRANS} >/tmp/gl-dual-wg-live-outcome.log 2>&1 < /dev/null" || true
if [[ "${RUN_MIXED}" -eq 1 ]]; then
  perf_remote_a "pkill -x iperf3 2>/dev/null || true"
  perf_remote_b "pkill -x iperf3 2>/dev/null || true"
  perf_start_iperf_tcp_server "dual-wg-stable-mixed-tcp" "${PORT_A}" "10.205.0.1" 7811
  perf_start_iperf_udp_server "dual-wg-fast-mixed-udp" "${PORT_A}" "10.206.0.1" 7812
  sleep 1
  perf_start_iperf_tcp_client_background "dual-wg-stable-mixed-tcp" "${PORT_B}" "10.205.0.1" 7811 "${PARALLEL}" "${DURATION}"
  perf_start_iperf_udp_client_background "dual-wg-fast-mixed-udp" "${PORT_B}" "10.206.0.1" 7812 "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
  sleep $((DURATION + 2))
  perf_fetch_iperf_tcp_background "dual-wg-stable-mixed-tcp" "${PORT_A}" "${PORT_B}"
  perf_fetch_iperf_udp_background "dual-wg-fast-mixed-udp" "${PORT_A}" "${PORT_B}"
else
  perf_run_iperf_tcp "dual-wg-stable-tcp" "${PORT_A}" "${PORT_B}" "10.205.0.1" "10.205.0.1" 7811 "${PARALLEL}" "${DURATION}"
  perf_run_iperf_udp "dual-wg-fast-udp" "${PORT_A}" "${PORT_B}" "10.206.0.1" "10.206.0.1" 7812 "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
fi
perf_remote_a "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink services status gl-dual-wg.vm.node-a" >"${OUT_DIR}/status-node-a.json" || true
perf_remote_b "cd ${PERF_REMOTE_REPO} && .venv/bin/gatherlink services status gl-dual-wg.vm.node-b" >"${OUT_DIR}/status-node-b.json" || true
perf_remote_b "cat /tmp/gl-dual-wg-live-outcome.log 2>/dev/null || true" >"${OUT_DIR}/live-service-outcome.log" || true
perf_fetch_node_probe "dualwg-node-a" "${PORT_A}"
perf_fetch_node_probe "dualwg-node-b" "${PORT_B}"

perf_step "Summary"
perf_summarize_iperf_jsons | tee -a "${REPORT}"
PYTHONPATH=python .venv/bin/python - "${REPORT_JSON}" "${OUTCOME_TCP_MIN_MBIT}" "${OUTCOME_TCP_MAX_RETRANS}" "${OUTCOME_UDP_MAX_LOSS_PERCENT}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from gatherlink.scheduling.service_outcome import (
    DualWireGuardOutcomeThresholds,
    dual_wireguard_outcome_from_results,
    outcome_snapshot_to_report,
)

report_path = Path(sys.argv[1])
tcp_min_text = sys.argv[2]
tcp_retrans_text = sys.argv[3]
udp_loss_text = sys.argv[4]
report = json.loads(report_path.read_text(encoding="utf-8"))
thresholds = DualWireGuardOutcomeThresholds(
    tcp_min_mbit_per_second=float(tcp_min_text) if tcp_min_text else None,
    tcp_max_retransmits=int(tcp_retrans_text) if tcp_retrans_text else None,
    udp_max_loss_percent=float(udp_loss_text) if udp_loss_text else None,
)
snapshot = dual_wireguard_outcome_from_results(report.get("results", []), thresholds=thresholds)
report["service_outcomes"] = outcome_snapshot_to_report(snapshot)
report["service_outcome_thresholds"] = {
    "tcp_min_mbit_per_second": thresholds.tcp_min_mbit_per_second,
    "tcp_max_retransmits": thresholds.tcp_max_retransmits,
    "udp_max_loss_percent": thresholds.udp_max_loss_percent,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
for outcome in report["service_outcomes"]:
    status = "degraded" if outcome["degraded"] else "ok"
    print(f"- service-outcome {outcome['service']}: {status} {outcome['reason']}".rstrip())
PY
.venv/bin/python - "${REPORT_JSON}" <<'PY' | tee -a "${REPORT}"
from __future__ import annotations

import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
outcomes = report.get("service_outcomes", [])
if outcomes:
    print("")
    print("## Service Outcomes")
    for outcome in outcomes:
        status = "degraded" if outcome.get("degraded") else "ok"
        reason = outcome.get("reason") or ""
        print(f"- {outcome.get('service')}: {status} {reason}".rstrip())
else:
    print("")
    print("## Service Outcomes")
    print("- no degraded service outcomes detected")
PY
perf_record ""
perf_record "WireGuard helper plan: captured in VM B /tmp/gl-dual-wireguard-plan.txt"
perf_record "Traffic split dry-run: captured in VM B /tmp/gl-dual-traffic-split.txt"
perf_record "JSON summary: ${REPORT_JSON}"
if [[ "${PERF_KEEP_RUNNING}" -eq 1 ]]; then
  perf_record "Gatherlink services and WireGuard interfaces were left running."
fi
printf '\nDual WireGuard over Gatherlink speed complete.\nReport: %s\n' "${REPORT}"
