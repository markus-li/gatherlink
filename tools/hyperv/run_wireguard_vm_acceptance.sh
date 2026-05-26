#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/vm_ip_cache.sh"

PLINK="${PLINK:-/mnt/c/Progra~1/PuTTY/plink.exe}"
TRANSPORT="${TRANSPORT:-plink}"
BRANCH="$(cd "${REPO_ROOT}" && git rev-parse --abbrev-ref HEAD)"
VM_A="gatherlink-vm-a"
VM_B="gatherlink-vm-b"
IP_A=""
IP_B=""
PORT_A=""
PORT_B=""
HOST_KEY_A=""
HOST_KEY_B=""
BUILD_RUST=0
INVENTORY=""
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-wireguard-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage: run_wireguard_vm_acceptance.sh --host-key-a KEY --host-key-b KEY [options]

Proves the v0.9 WireGuard helper contract in the two-Debian-VM Hyper-V lab:

1. render the WireGuard-over-Gatherlink plan on VM A
2. point a WireGuard-style UDP sender at the planned Gatherlink Endpoint
3. carry that UDP datagram through real Gatherlink per-path carrier sockets
4. receive it at the peer-side WireGuard UDP target on VM B

This does not create WireGuard interfaces. WireGuard owns interfaces, keys,
routes, and firewall policy; Gatherlink proves the UDP transport endpoint it
provides for WireGuard.

Options:
  --inventory FILE     Optional env file with HYPERV_VM_* values.
  --ip-a IP            VM A management IP. If omitted, resolve via Hyper-V helper.
  --ip-b IP            VM B management IP. If omitted, resolve via Hyper-V helper.
  --port-a PORT        Optional SSH port for VM A when using a shared portproxy IP.
  --port-b PORT        Optional SSH port for VM B when using a shared portproxy IP.
  --host-key-a KEY     PuTTY host-key fingerprint for VM A.
  --host-key-b KEY     PuTTY host-key fingerprint for VM B.
  --transport NAME     SSH transport: plink or ssh. Default plink.
  --branch NAME        Branch to push. Defaults current branch.
  --out DIR            Report directory.
  --build              Reinstall Python deps and rebuild PyO3 binding on VMs.
USAGE
}

for ((index = 1; index <= $#; index++)); do
  if [[ "${!index}" == "--inventory" ]]; then
    next_index=$((index + 1))
    INVENTORY="${!next_index:-}"
    break
  fi
done

if [[ -n "${INVENTORY}" ]]; then
  # shellcheck disable=SC1090
  source "${INVENTORY}"
  VM_A="${HYPERV_VM_A:-${VM_A}}"
  VM_B="${HYPERV_VM_B:-${VM_B}}"
  IP_A="${HYPERV_VM_A_IP:-${IP_A}}"
  IP_B="${HYPERV_VM_B_IP:-${IP_B}}"
  PORT_A="${HYPERV_VM_A_PORT:-${PORT_A}}"
  PORT_B="${HYPERV_VM_B_PORT:-${PORT_B}}"
  HOST_KEY_A="${HYPERV_VM_A_HOST_KEY:-${HOST_KEY_A}}"
  HOST_KEY_B="${HYPERV_VM_B_HOST_KEY:-${HOST_KEY_B}}"
  BRANCH="${HYPERV_BRANCH:-${BRANCH}}"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --ip-a) IP_A="$2"; shift 2 ;;
    --ip-b) IP_B="$2"; shift 2 ;;
    --port-a) PORT_A="$2"; shift 2 ;;
    --port-b) PORT_B="$2"; shift 2 ;;
    --host-key-a) HOST_KEY_A="$2"; shift 2 ;;
    --host-key-b) HOST_KEY_B="$2"; shift 2 ;;
    --transport) TRANSPORT="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --build) BUILD_RUST=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "${TRANSPORT}" in
  plink)
    [[ -n "${HOST_KEY_A}" ]] || { echo "--host-key-a is required" >&2; exit 2; }
    [[ -n "${HOST_KEY_B}" ]] || { echo "--host-key-b is required" >&2; exit 2; }
    [[ -x "${PLINK}" ]] || { echo "plink not found at ${PLINK}" >&2; exit 2; }
    ;;
  ssh) ;;
  *) echo "--transport must be plink or ssh" >&2; exit 2 ;;
esac

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

remote() {
  local label="$1"
  local ip="$2"
  local port="$3"
  local host_key="$4"
  local command="$5"
  local port_args=()
  if [[ "${TRANSPORT}" == "ssh" ]]; then
    if [[ -n "${port}" ]]; then
      port_args=(-p "${port}")
    fi
    log_cmd "${label}" "ssh ${ip}${port:+:${port}} ${command}"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${port_args[@]}" "gatherlink@${ip}" "${command}"
  else
    if [[ -n "${port}" ]]; then
      port_args=(-P "${port}")
    fi
    log_cmd "${label}" "plink ${ip}${port:+:${port}} ${command}"
    "${PLINK}" -batch -agent -hostkey "${host_key}" "${port_args[@]}" -l gatherlink "${ip}" "${command}"
  fi
}

remote_a() { remote "$1" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "$2"; }
remote_b() { remote "$1" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "$2"; }

sync_node() {
  local label="$1"
  local ip="$2"
  local port="$3"
  local host_key="$4"
  local git_url="ssh://gatherlink@${ip}${port:+:${port}}/home/gatherlink/repos/gatherlink.git"
  remote "${label}-prepare-repo" "${ip}" "${port}" "${host_key}" \
    "mkdir -p /home/gatherlink/repos && if [ ! -d /home/gatherlink/repos/gatherlink.git ]; then git init --bare /home/gatherlink/repos/gatherlink.git; fi && git --git-dir=/home/gatherlink/repos/gatherlink.git symbolic-ref HEAD refs/heads/${BRANCH} || true"
  log_cmd "${label}-push" "git push ${git_url} HEAD:${BRANCH}"
  (
    cd "${REPO_ROOT}"
    if [[ "${TRANSPORT}" == "ssh" ]]; then
      GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
        git push --force "${git_url}" "HEAD:refs/heads/${BRANCH}"
    else
      local git_port_args=""
      if [[ -n "${port}" ]]; then
        git_port_args=" -P ${port}"
      fi
      GIT_SSH_COMMAND="${PLINK} -batch -agent -hostkey ${host_key}${git_port_args}" \
        git push --force "ssh://gatherlink@${ip}/home/gatherlink/repos/gatherlink.git" "HEAD:refs/heads/${BRANCH}"
    fi
  )
  local install_command=""
  if [[ "${BUILD_RUST}" -eq 1 ]]; then
    install_command=" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt >/tmp/gatherlink-pip-install.log && .venv/bin/pip install -e . >>/tmp/gatherlink-pip-install.log && .venv/bin/maturin develop --manifest-path crates/pybindings/Cargo.toml --release >/tmp/gatherlink-maturin.log"
  fi
  remote "${label}-checkout" "${ip}" "${port}" "${host_key}" \
    "mkdir -p /home/gatherlink/src && if [ ! -d /home/gatherlink/src/gatherlink/.git ]; then rm -rf /home/gatherlink/src/gatherlink && git clone /home/gatherlink/repos/gatherlink.git /home/gatherlink/src/gatherlink; fi && cd /home/gatherlink/src/gatherlink && git fetch origin && git reset --hard origin/${BRANCH}${install_command}"
}

cleanup() {
  remote_a "cleanup-a" "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close wg.vm.node-a >/dev/null 2>&1 || true)"
  remote_b "cleanup-b" "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close wg.vm.node-b >/dev/null 2>&1 || true)"
}

trap cleanup EXIT

cat >"${REPORT}" <<REPORT
# Hyper-V WireGuard Over Gatherlink Acceptance

- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- branch: ${BRANCH}
- vm_a: ${VM_A} ${IP_A}
- vm_b: ${VM_B} ${IP_B}

REPORT

step "Sync"
sync_node "node-a" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}"
sync_node "node-b" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}"
record "source synced by Git to both VMs"

step "Prepare Configs"
remote_a "write-node-a-config" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path('configs/hyperv/two-vm-node-a.json').read_text())
cfg['services'][0]['name'] = 'wireguard-main'
cfg['helpers'] = {'wireguard': {'enabled': True, 'service': 'wireguard-main'}}
Path('/tmp/wireguard-node-a.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"
remote_b "write-node-b-config" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path('configs/hyperv/two-vm-node-b.json').read_text())
cfg['services'][0]['name'] = 'wireguard-main'
Path('/tmp/wireguard-node-b.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"
remote_a "validate-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/wireguard-node-a.json"
remote_b "validate-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/wireguard-node-b.json"
record "generated WireGuard helper configs validate"

step "Plan"
remote_a "wireguard-plan" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink helpers wireguard-plan /tmp/wireguard-node-a.json --diagnostics-jsonl /tmp/wireguard-plan.jsonl | tee /tmp/wireguard-plan.txt"
remote_a "wireguard-plan-verify" "grep -q 'service: wireguard-main' /tmp/wireguard-plan.txt && grep -q 'Endpoint = 127.0.0.1:55180' /tmp/wireguard-plan.txt"
record "WireGuard helper plan points the peer Endpoint at VM A's local Gatherlink service"

step "Start"
cleanup
remote_b "start-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/wireguard-node-b.json --name wg.vm.node-b --diagnostics-jsonl /tmp/wireguard-node-b.jsonl"
sleep 1
remote_a "start-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/wireguard-node-a.json --name wg.vm.node-a --diagnostics-jsonl /tmp/wireguard-node-a.jsonl"
sleep 2
record "both core services started through the normal managed runner"

step "WireGuard UDP Transport Proof"
remote_b "receiver" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/wireguard-received.txt; (timeout 20 .venv/bin/python tools/udp_probe.py receive 127.0.0.1:51820 --count 3 > /tmp/wireguard-received.txt 2>&1 & echo \$! > /tmp/wireguard-receiver.pid)"
sleep 1
remote_a "send" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/udp_probe.py send 127.0.0.1:55180 wireguard-vm-proof --count 3"
sleep 2
remote_b "verify" "test \$(grep -c '^wireguard-vm-proof' /tmp/wireguard-received.txt) -eq 3 && cat /tmp/wireguard-received.txt"
record "WireGuard-style UDP payloads sent to the planned Endpoint exited at VM B's WireGuard target port"

step "Counters And Diagnostics"
remote_a "status-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status wg.vm.node-a" | tee "${OUT_DIR}/status-node-a.json" >/dev/null
remote_b "status-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status wg.vm.node-b" | tee "${OUT_DIR}/status-node-b.json" >/dev/null
remote_a "copy-plan-diag" "cat /tmp/wireguard-plan.jsonl" >"${OUT_DIR}/wireguard-plan.jsonl"
remote_a "copy-node-a-diag" "cat /tmp/wireguard-node-a.jsonl" >"${OUT_DIR}/wireguard-node-a.jsonl"
remote_b "copy-node-b-diag" "cat /tmp/wireguard-node-b.jsonl" >"${OUT_DIR}/wireguard-node-b.jsonl"
grep -q 'helper.wireguard.plan' "${OUT_DIR}/wireguard-plan.jsonl"
record "core status and WireGuard plan diagnostics captured"

step "Result"
record "PASS: WireGuard helper plan and UDP transport endpoint were proven across the two VMs"
echo "Hyper-V WireGuard acceptance passed. Report: ${REPORT}"
