#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/vm_ip_cache.sh"

PLINK="${PLINK:-/mnt/c/Progra~1/PuTTY/plink.exe}"
BRANCH="$(cd "${REPO_ROOT}" && git rev-parse --abbrev-ref HEAD)"
VM_A="gatherlink-vm-a"
VM_B="gatherlink-vm-b"
VM_C="gatherlink-vm-c"
IP_A=""
IP_B=""
IP_C=""
HOST_KEY_A=""
HOST_KEY_B=""
HOST_KEY_C=""
BUILD_RUST=1
KEEP_RUNNING=0
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-relay-wireguard-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage: run_relay_wireguard_vm_acceptance.sh --host-key-a KEY --host-key-b KEY --host-key-c KEY [options]

Runs a three-VM relay acceptance proof:
  VM B WireGuard peer -> Gatherlink core on B -> untrusted relay VM C ->
  final-hop relay exit on VM A -> Gatherlink core on A -> VM A WireGuard peer.

The proof curls the VM A status HTTP helper from VM B through a real WireGuard
interface. Gatherlink still owns only the UDP transport endpoint; WireGuard owns
the WireGuard interface, keys, routes, and packet format.

Options:
  --ip-a IP            VM A management IP. If omitted, use the VM IP cache/resolver.
  --ip-b IP            VM B management IP. If omitted, use the VM IP cache/resolver.
  --ip-c IP            VM C management IP. If omitted, use the VM IP cache/resolver.
  --host-key-a KEY     PuTTY host-key fingerprint for VM A.
  --host-key-b KEY     PuTTY host-key fingerprint for VM B.
  --host-key-c KEY     PuTTY host-key fingerprint for VM C.
  --branch NAME        Branch to push. Defaults current branch.
  --out DIR            Report directory.
  --skip-build         Sync source but skip pip/maturin install.
  --keep-running       Leave core, relay, WireGuard, and HTTP services running.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip-a) IP_A="$2"; shift 2 ;;
    --ip-b) IP_B="$2"; shift 2 ;;
    --ip-c) IP_C="$2"; shift 2 ;;
    --host-key-a) HOST_KEY_A="$2"; shift 2 ;;
    --host-key-b) HOST_KEY_B="$2"; shift 2 ;;
    --host-key-c) HOST_KEY_C="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --skip-build) BUILD_RUST=0; shift ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "${HOST_KEY_A}" ]] || { echo "--host-key-a is required" >&2; exit 2; }
[[ -n "${HOST_KEY_B}" ]] || { echo "--host-key-b is required" >&2; exit 2; }
[[ -n "${HOST_KEY_C}" ]] || { echo "--host-key-c is required" >&2; exit 2; }
[[ -x "${PLINK}" ]] || { echo "plink not found at ${PLINK}" >&2; exit 2; }

mkdir -p "${OUT_DIR}"
REPORT="${OUT_DIR}/report.md"
COMMAND_LOG="${OUT_DIR}/commands.log"
: >"${COMMAND_LOG}"

log_cmd() {
  printf '[%s] %s\n' "$1" "$2" >>"${COMMAND_LOG}"
}

step() {
  printf '\n## %s\n\n' "$1" | tee -a "${REPORT}"
}

record() {
  printf -- '- %s\n' "$1" | tee -a "${REPORT}"
}

IP_A="$(hyperv_resolve_vm_ip "${REPO_ROOT}" "${SCRIPT_DIR}" "${VM_A}" "${IP_A}")"
IP_B="$(hyperv_resolve_vm_ip "${REPO_ROOT}" "${SCRIPT_DIR}" "${VM_B}" "${IP_B}")"
IP_C="$(hyperv_resolve_vm_ip "${REPO_ROOT}" "${SCRIPT_DIR}" "${VM_C}" "${IP_C}")"

remote() {
  local label="$1"
  local ip="$2"
  local host_key="$3"
  local command="$4"
  log_cmd "${label}" "plink ${ip} ${command}"
  "${PLINK}" -batch -agent -hostkey "${host_key}" -l gatherlink "${ip}" "${command}"
}

remote_capture() {
  local label="$1"
  local ip="$2"
  local host_key="$3"
  local command="$4"
  local output="$5"
  log_cmd "${label}" "plink ${ip} ${command} > ${output}"
  "${PLINK}" -batch -agent -hostkey "${host_key}" -l gatherlink "${ip}" "${command}" >"${output}"
}

remote_a() { remote "$1" "${IP_A}" "${HOST_KEY_A}" "$2"; }
remote_b() { remote "$1" "${IP_B}" "${HOST_KEY_B}" "$2"; }
remote_c() { remote "$1" "${IP_C}" "${HOST_KEY_C}" "$2"; }
remote_capture_a() { remote_capture "$1" "${IP_A}" "${HOST_KEY_A}" "$2" "$3"; }
remote_capture_b() { remote_capture "$1" "${IP_B}" "${HOST_KEY_B}" "$2" "$3"; }
remote_capture_c() { remote_capture "$1" "${IP_C}" "${HOST_KEY_C}" "$2" "$3"; }

sync_node() {
  local label="$1"
  local ip="$2"
  local host_key="$3"
  remote "${label}-prepare-repo" "${ip}" "${host_key}" \
    "mkdir -p /home/gatherlink/repos && if [ ! -d /home/gatherlink/repos/gatherlink.git ]; then git init --bare /home/gatherlink/repos/gatherlink.git; fi && git --git-dir=/home/gatherlink/repos/gatherlink.git symbolic-ref HEAD refs/heads/${BRANCH} || true"
  log_cmd "${label}-push" "git push ssh://gatherlink@${ip}/home/gatherlink/repos/gatherlink.git HEAD:${BRANCH}"
  (
    cd "${REPO_ROOT}"
    GIT_SSH_COMMAND="${PLINK} -batch -agent -hostkey ${host_key}" \
      git push --force "ssh://gatherlink@${ip}/home/gatherlink/repos/gatherlink.git" "HEAD:refs/heads/${BRANCH}"
  )
  local install_command=""
  if [[ "${BUILD_RUST}" -eq 1 ]]; then
    install_command=" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt >/tmp/gatherlink-pip-install.log && .venv/bin/pip install -e . >>/tmp/gatherlink-pip-install.log && .venv/bin/maturin develop --manifest-path crates/pybindings/Cargo.toml --release >/tmp/gatherlink-maturin.log"
  fi
  remote "${label}-checkout" "${ip}" "${host_key}" \
    "mkdir -p /home/gatherlink/src && if [ ! -d /home/gatherlink/src/gatherlink/.git ]; then rm -rf /home/gatherlink/src/gatherlink && git clone /home/gatherlink/repos/gatherlink.git /home/gatherlink/src/gatherlink; fi && cd /home/gatherlink/src/gatherlink && git fetch origin && git reset --hard origin/${BRANCH}${install_command}"
}

cleanup_node() {
  local label="$1"
  local ip="$2"
  local host_key="$3"
  remote "${label}-cleanup" "${ip}" "${host_key}" \
    "cd /home/gatherlink/src/gatherlink && if [ -x .venv/bin/gatherlink ]; then .venv/bin/gatherlink services list | awk '/^[^[:space:]:]+[[:space:]]/ && \$0 !~ /kind=remote/ {print \$1}' | while read -r service; do [ -n \"\${service}\" ] && .venv/bin/gatherlink services close \"\${service}\" >/dev/null 2>&1 || true; done; fi; pkill -f '[h]elpers status-http' || true; sudo ip link del wg-gl-a 2>/dev/null || true; sudo ip link del wg-gl-b 2>/dev/null || true; for path in path-a path-b path-c; do sudo tc qdisc del dev \${path} root 2>/dev/null || true; sudo ip link set \${path} up; done"
}

stop_all() {
  cleanup_node "node-a-stop" "${IP_A}" "${HOST_KEY_A}" >/dev/null 2>&1 || true
  cleanup_node "node-b-stop" "${IP_B}" "${HOST_KEY_B}" >/dev/null 2>&1 || true
  cleanup_node "node-c-stop" "${IP_C}" "${HOST_KEY_C}" >/dev/null 2>&1 || true
}

if [[ "${KEEP_RUNNING}" -eq 0 ]]; then
  trap stop_all EXIT
fi

cat >"${REPORT}" <<REPORT
# Hyper-V Relay WireGuard Acceptance

- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- branch: ${BRANCH}
- vm_a_final_sink: ${VM_A} ${IP_A}
- vm_b_source: ${VM_B} ${IP_B}
- vm_c_untrusted_relay: ${VM_C} ${IP_C}
- keep_running: ${KEEP_RUNNING}

REPORT

step "Prerequisites"
for remote_name in a b c; do
  "remote_${remote_name}" "prereq-${remote_name}" \
    "command -v wg >/dev/null && command -v curl >/dev/null && sudo -n true >/dev/null"
done
record "WireGuard tools, curl, and passwordless lab sudo are available on all three VMs"

step "Sync And Build"
sync_node "node-a" "${IP_A}" "${HOST_KEY_A}"
sync_node "node-b" "${IP_B}" "${HOST_KEY_B}"
sync_node "node-c" "${IP_C}" "${HOST_KEY_C}"
record "source synced by Git and VM working trees reset to ${BRANCH}"

step "Prepare Runtime Configs"
cleanup_node "node-a" "${IP_A}" "${HOST_KEY_A}"
cleanup_node "node-b" "${IP_B}" "${HOST_KEY_B}"
cleanup_node "node-c" "${IP_C}" "${HOST_KEY_C}"
remote_b "generate-wireguard-b" "wg genkey | tee /tmp/wg-relay-b.key | wg pubkey > /tmp/wg-relay-b.pub"
remote_a "generate-wireguard-a" "wg genkey | tee /tmp/wg-relay-a.key | wg pubkey > /tmp/wg-relay-a.pub"
WG_B_PUB="$(remote_capture_b "read-wireguard-b-pub" "cat /tmp/wg-relay-b.pub" "${OUT_DIR}/wg-b.pub"; tr -d '\r\n' <"${OUT_DIR}/wg-b.pub")"
WG_A_PUB="$(remote_capture_a "read-wireguard-a-pub" "cat /tmp/wg-relay-a.pub" "${OUT_DIR}/wg-a.pub"; tr -d '\r\n' <"${OUT_DIR}/wg-a.pub")"

remote_a "write-configs-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

paths = []
for index in range(1, 4):
    paths.append(
        {
            'name': f'path-{chr(96 + index)}',
            'interface': f'path-{chr(96 + index)}',
            'transport_bind': f'10.91.{index}.11:{61100 + index}',
            'transport_remote': f'10.91.{index}.13:{62100 + index}',
            'relay': {'relay_receiver_index': 7200 + index, 'send_key': key(0x50 + index)},
            'scheduler': {'mtu': 1200, 'tx_capacity_bps': 3000000, 'rx_capacity_bps': 3000000},
        }
    )
cfg = {
    'schema_version': 1,
    'node': 'relaywg-node-a-final',
    'role': 'server',
    'peer': 'relaywg-node-b-source',
    'paths': paths,
    'services': [
        {
            'name': 'wireguard-main',
            'listen': '0.0.0.0:0',
            'target': '127.0.0.1:51830',
            'return_mode': 'peer-scoped-source',
        }
    ],
    'helpers': {'wireguard': {'enabled': True, 'service': 'wireguard-main'}},
    'security': {
        'mode': 'authenticated',
        'local_receiver_index': 601,
        'remote_receiver_index': 501,
        'send_key': key(0x42),
        'receive_key': key(0x41),
    },
}
Path('/tmp/relaywg-node-a.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
for index in range(1, 4):
    exit_cfg = {
        'schema_version': 1,
        'name': f'ba-exit-path-{chr(96 + index)}',
        'listen': f'10.91.{index}.11:{63000 + index}',
        'exit_to_inner_packet': True,
        'executor': {
            'relay_receiver_index': 8100 + index,
            'next_hop_transport': 'udp',
            'next_hop_address': f'10.91.{index}.11:{61100 + index}',
            'next_hop_receiver_index': 0,
            'direction': 'upstream_to_downstream',
            'topology_generation': 1,
            'expires_at_unix_us': 4102444800000000,
            'max_packet_size': 4096,
        },
        'keys': {'send_key': key(0), 'receive_key': key(0x60 + index)},
    }
    Path(f'/tmp/relaywg-a-exit-ba-path-{chr(96 + index)}.json').write_text(json.dumps(exit_cfg, indent=2, sort_keys=True))
PY"

remote_b "write-configs-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

paths = []
for index in range(1, 4):
    paths.append(
        {
            'name': f'path-{chr(96 + index)}',
            'interface': f'path-{chr(96 + index)}',
            'transport_bind': f'10.91.{index}.12:{61000 + index}',
            'transport_remote': f'10.91.{index}.13:{62000 + index}',
            'relay': {'relay_receiver_index': 7100 + index, 'send_key': key(0x40 + index)},
            'scheduler': {'mtu': 1200, 'tx_capacity_bps': 3000000, 'rx_capacity_bps': 3000000},
        }
    )
cfg = {
    'schema_version': 1,
    'node': 'relaywg-node-b-source',
    'role': 'client',
    'peer': 'relaywg-node-a-final',
    'paths': paths,
    'services': [
        {
            'name': 'wireguard-main',
            'listen': '127.0.0.1:55180',
            'target': '127.0.0.1:51831',
            'return_mode': 'learned-single-source',
        }
    ],
    'helpers': {'wireguard': {'enabled': True, 'service': 'wireguard-main'}},
    'security': {
        'mode': 'authenticated',
        'local_receiver_index': 501,
        'remote_receiver_index': 601,
        'send_key': key(0x41),
        'receive_key': key(0x42),
    },
}
Path('/tmp/relaywg-node-b.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
for index in range(1, 4):
    exit_cfg = {
        'schema_version': 1,
        'name': f'ab-exit-path-{chr(96 + index)}',
        'listen': f'10.91.{index}.12:{63100 + index}',
        'exit_to_inner_packet': True,
        'executor': {
            'relay_receiver_index': 8200 + index,
            'next_hop_transport': 'udp',
            'next_hop_address': f'10.91.{index}.12:{61000 + index}',
            'next_hop_receiver_index': 0,
            'direction': 'downstream_to_upstream',
            'topology_generation': 1,
            'expires_at_unix_us': 4102444800000000,
            'max_packet_size': 4096,
        },
        'keys': {'send_key': key(0), 'receive_key': key(0x70 + index)},
    }
    Path(f'/tmp/relaywg-b-exit-ab-path-{chr(96 + index)}.json').write_text(json.dumps(exit_cfg, indent=2, sort_keys=True))
PY"

remote_c "write-configs-c" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import base64
import json
from pathlib import Path

def key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()

for index in range(1, 4):
    path_name = f'path-{chr(96 + index)}'
    ba_cfg = {
        'schema_version': 1,
        'name': f'ba-relay-{path_name}',
        'listen': f'10.91.{index}.13:{62000 + index}',
        'executor': {
            'relay_receiver_index': 7100 + index,
            'next_hop_transport': 'udp',
            'next_hop_address': f'10.91.{index}.11:{63000 + index}',
            'next_hop_receiver_index': 8100 + index,
            'direction': 'upstream_to_downstream',
            'topology_generation': 1,
            'expires_at_unix_us': 4102444800000000,
            'max_packet_size': 4096,
        },
        'keys': {'send_key': key(0x60 + index), 'receive_key': key(0x40 + index)},
    }
    ab_cfg = {
        'schema_version': 1,
        'name': f'ab-relay-{path_name}',
        'listen': f'10.91.{index}.13:{62100 + index}',
        'executor': {
            'relay_receiver_index': 7200 + index,
            'next_hop_transport': 'udp',
            'next_hop_address': f'10.91.{index}.12:{63100 + index}',
            'next_hop_receiver_index': 8200 + index,
            'direction': 'downstream_to_upstream',
            'topology_generation': 1,
            'expires_at_unix_us': 4102444800000000,
            'max_packet_size': 4096,
        },
        'keys': {'send_key': key(0x70 + index), 'receive_key': key(0x50 + index)},
    }
    Path(f'/tmp/relaywg-c-ba-{path_name}.json').write_text(json.dumps(ba_cfg, indent=2, sort_keys=True))
    Path(f'/tmp/relaywg-c-ab-{path_name}.json').write_text(json.dumps(ab_cfg, indent=2, sort_keys=True))
PY"

remote_a "validate-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/relaywg-node-a.json"
remote_b "validate-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/relaywg-node-b.json"
record "relay-wrapped endpoint configs validate on B and A"

step "Start Relay Topology"
remote_a "configure-wireguard-a" "sudo ip link del wg-gl-a 2>/dev/null || true; sudo ip link add wg-gl-a type wireguard; sudo ip addr replace 10.202.0.1/24 dev wg-gl-a; sudo wg set wg-gl-a private-key /tmp/wg-relay-a.key listen-port 51830 peer '${WG_B_PUB}' allowed-ips 10.202.0.2/32; sudo ip link set mtu 1280 up dev wg-gl-a"
remote_b "configure-wireguard-b" "sudo ip link del wg-gl-b 2>/dev/null || true; sudo ip link add wg-gl-b type wireguard; sudo ip addr replace 10.202.0.2/24 dev wg-gl-b; sudo wg set wg-gl-b private-key /tmp/wg-relay-b.key listen-port 51831 peer '${WG_A_PUB}' allowed-ips 10.202.0.1/32 endpoint 127.0.0.1:55180 persistent-keepalive 5; sudo ip link set mtu 1280 up dev wg-gl-b"
remote_a "start-status-http-a" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers status-http --listen 10.202.0.1:18081 --allow-non-loopback --write-window-seconds 0 >/tmp/relaywg-status-http.log 2>&1 </dev/null & echo \$! >/tmp/relaywg-status-http.pid)"

for path in a b c; do
  remote_a "start-a-exit-${path}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run relay-start /tmp/relaywg-a-exit-ba-path-${path}.json --name relaywg.a.exit.ba.path-${path} --diagnostics-jsonl /tmp/relaywg-a-exit-ba-path-${path}.jsonl"
  remote_b "start-b-exit-${path}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run relay-start /tmp/relaywg-b-exit-ab-path-${path}.json --name relaywg.b.exit.ab.path-${path} --diagnostics-jsonl /tmp/relaywg-b-exit-ab-path-${path}.jsonl"
  remote_c "start-c-relay-ba-${path}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run relay-start /tmp/relaywg-c-ba-path-${path}.json --name relaywg.c.relay.ba.path-${path} --diagnostics-jsonl /tmp/relaywg-c-ba-path-${path}.jsonl"
  remote_c "start-c-relay-ab-${path}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run relay-start /tmp/relaywg-c-ab-path-${path}.json --name relaywg.c.relay.ab.path-${path} --diagnostics-jsonl /tmp/relaywg-c-ab-path-${path}.jsonl"
done
remote_a "start-core-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/relaywg-node-a.json --name relaywg.vm.node-a --diagnostics-jsonl /tmp/relaywg-node-a.jsonl"
sleep 1
remote_b "start-core-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/relaywg-node-b.json --name relaywg.vm.node-b --diagnostics-jsonl /tmp/relaywg-node-b.jsonl"
sleep 3
record "B core, C relay services, A final-hop exits, A core, WireGuard interfaces, and A HTTP helper started"

step "Curl Through WireGuard Over Relayed Gatherlink"
remote_b "curl-through-wireguard" "for attempt in \$(seq 1 20); do curl --interface wg-gl-b --max-time 8 -sS http://10.202.0.1:18081/text > /tmp/relaywg-curl.txt && grep -q 'Gatherlink local status (EXPERIMENTAL)' /tmp/relaywg-curl.txt && break; sleep 1; done; cat /tmp/relaywg-curl.txt; grep -q 'Gatherlink local status (EXPERIMENTAL)' /tmp/relaywg-curl.txt"
remote_b "wireguard-show-b" "sudo wg show wg-gl-b" | tee "${OUT_DIR}/wg-show-b.txt" >/dev/null
remote_a "wireguard-show-a" "sudo wg show wg-gl-a" | tee "${OUT_DIR}/wg-show-a.txt" >/dev/null
record "VM B fetched VM A HTTP status through WireGuard carried by Gatherlink via untrusted relay VM C"

step "Operator Graph Views"
remote_capture_b "monitor-b-graph" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor relaywg.vm.node-b relaywg.b.exit.ab.path-a relaywg.b.exit.ab.path-b relaywg.b.exit.ab.path-c --view graph --once" "${OUT_DIR}/monitor-b-graph.txt"
remote_capture_c "monitor-c-graph" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor relaywg.c.relay.ba.path-a relaywg.c.relay.ba.path-b relaywg.c.relay.ba.path-c relaywg.c.relay.ab.path-a relaywg.c.relay.ab.path-b relaywg.c.relay.ab.path-c --view graph --once" "${OUT_DIR}/monitor-c-graph.txt"
remote_capture_a "monitor-a-graph" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor relaywg.vm.node-a relaywg.a.exit.ba.path-a relaywg.a.exit.ba.path-b relaywg.a.exit.ba.path-c --view graph --once" "${OUT_DIR}/monitor-a-graph.txt"
grep -q "dependency graph" "${OUT_DIR}/monitor-b-graph.txt"
grep -q "dependency graph" "${OUT_DIR}/monitor-c-graph.txt"
grep -q "dependency graph" "${OUT_DIR}/monitor-a-graph.txt"
record "service monitor graph view captured on B, C, and A"

step "Diagnostics"
remote_capture_b "services-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-b.txt"
remote_capture_c "services-c" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-c.txt"
remote_capture_a "services-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-a.txt"
remote_capture_b "status-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status relaywg.vm.node-b" "${OUT_DIR}/status-b.json"
remote_capture_a "status-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status relaywg.vm.node-a" "${OUT_DIR}/status-a.json"
record "service lists and core status snapshots captured"

if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
  step "Left Running"
  record "services are still running for manual inspection; use the monitor commands in ${OUT_DIR}/commands.log"
else
  step "Cleanup"
  stop_all
  record "relay, core, HTTP, and WireGuard lab state cleaned up"
fi

step "Result"
record "PASS: B -> C -> A untrusted relay topology carried curl over a real WireGuard interface"
record "report: ${REPORT}"
record "command log: ${COMMAND_LOG}"
printf '\nRelay WireGuard acceptance complete.\nReport: %s\nCommands: %s\n' "${REPORT}" "${COMMAND_LOG}"
