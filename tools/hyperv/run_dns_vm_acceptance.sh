#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/vm_ip_cache.sh"

PLINK="${PLINK:-/mnt/c/Progra~1/PuTTY/plink.exe}"
BRANCH="$(cd "${REPO_ROOT}" && git rev-parse --abbrev-ref HEAD)"
VM_A="gatherlink-vm-a"
VM_B="gatherlink-vm-b"
IP_A=""
IP_B=""
HOST_KEY_A=""
HOST_KEY_B=""
BUILD_RUST=0
INVENTORY=""
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-dns-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage: run_dns_vm_acceptance.sh --host-key-a KEY --host-key-b KEY [options]

Proves DNS helper tunnel upstream behavior in the two-Debian-VM Hyper-V lab:

DNS client on VM A -> DNS helper on VM A -> local Gatherlink UDP service ->
Gatherlink core A/B -> static DNS endpoint on VM B -> response back to VM A.

Options:
  --inventory FILE     Optional env file with HYPERV_VM_* values.
  --ip-a IP            VM A management IP. If omitted, resolve via Hyper-V helper.
  --ip-b IP            VM B management IP. If omitted, resolve via Hyper-V helper.
  --host-key-a KEY     PuTTY host-key fingerprint for VM A.
  --host-key-b KEY     PuTTY host-key fingerprint for VM B.
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
  HOST_KEY_A="${HYPERV_VM_A_HOST_KEY:-${HOST_KEY_A}}"
  HOST_KEY_B="${HYPERV_VM_B_HOST_KEY:-${HOST_KEY_B}}"
  BRANCH="${HYPERV_BRANCH:-${BRANCH}}"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --ip-a) IP_A="$2"; shift 2 ;;
    --ip-b) IP_B="$2"; shift 2 ;;
    --host-key-a) HOST_KEY_A="$2"; shift 2 ;;
    --host-key-b) HOST_KEY_B="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --build) BUILD_RUST=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "${HOST_KEY_A}" ]] || { echo "--host-key-a is required" >&2; exit 2; }
[[ -n "${HOST_KEY_B}" ]] || { echo "--host-key-b is required" >&2; exit 2; }
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

remote() {
  local label="$1"
  local ip="$2"
  local host_key="$3"
  local command="$4"
  log_cmd "${label}" "plink ${ip} ${command}"
  "${PLINK}" -batch -agent -hostkey "${host_key}" -l gatherlink "${ip}" "${command}"
}

remote_a() { remote "$1" "${IP_A}" "${HOST_KEY_A}" "$2"; }
remote_b() { remote "$1" "${IP_B}" "${HOST_KEY_B}" "$2"; }

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

cleanup() {
  remote_a "cleanup-a" "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close dns.vm.node-a || true); pkill -f '[h]elpers dns-serve' || true"
  remote_b "cleanup-b" "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close dns.vm.node-b || true); pkill -f '[d]ns_static_server.py' || true"
}

trap cleanup EXIT

cat >"${REPORT}" <<REPORT
# Hyper-V DNS Tunnel Over Gatherlink Acceptance

- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- branch: ${BRANCH}
- vm_a: ${VM_A} ${IP_A}
- vm_b: ${VM_B} ${IP_B}

REPORT

step "Sync"
sync_node "node-a" "${IP_A}" "${HOST_KEY_A}"
sync_node "node-b" "${IP_B}" "${HOST_KEY_B}"
record "source synced by Git to both VMs"

step "Prepare Configs"
remote_a "write-node-a-config" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path('configs/hyperv/two-vm-node-a.json').read_text())
cfg['services'][0]['name'] = 'dns-main'
cfg['services'][0]['listen'] = '127.0.0.1:55153'
cfg['services'][0]['target'] = '127.0.0.1:53053'
cfg['services'][0]['return_mode'] = 'learned-single-source'
Path('/tmp/dns-node-a.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"
remote_b "write-node-b-config" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path('configs/hyperv/two-vm-node-b.json').read_text())
cfg['services'][0]['name'] = 'dns-main'
cfg['services'][0]['target'] = '127.0.0.1:53053'
Path('/tmp/dns-node-b.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"
remote_a "validate-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/dns-node-a.json"
remote_b "validate-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/dns-node-b.json"
record "generated DNS VM configs validate; VM A uses learned-single-source replies for the DNS helper"

step "Start"
cleanup
remote_b "start-static-dns" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/python tools/dns_static_server.py --listen 127.0.0.1:53053 --name vm-dns.gatherlink.test. --address 192.0.2.77 >/tmp/dns-static-server.log 2>&1 </dev/null & echo \$! >/tmp/dns-static-server.pid)"
remote_b "start-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/dns-node-b.json --name dns.vm.node-b --diagnostics-jsonl /tmp/dns-node-b.jsonl"
sleep 1
remote_a "start-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/dns-node-a.json --name dns.vm.node-a --diagnostics-jsonl /tmp/dns-node-a.jsonl"
sleep 2
remote_a "start-dns-helper" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers dns-serve --listen 127.0.0.1:5353 --tunnel-upstream peer-dns=127.0.0.1:55153,timeout=2 --diagnostics-jsonl /tmp/dns-helper.jsonl >/tmp/dns-helper.log 2>&1 </dev/null & echo \$! >/tmp/dns-helper.pid)"
sleep 1
record "static DNS endpoint, both core services, and the VM A DNS helper started"

step "DNS Query Probe"
remote_a "dns-probe" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/dns_probe.py --server 127.0.0.1:5353 --name vm-dns.gatherlink.test. --expect 192.0.2.77 --timeout 10 | tee /tmp/dns-probe.txt"
remote_a "dns-probe-verify" "grep -q '192.0.2.77' /tmp/dns-probe.txt"
record "DNS query to VM A helper returned the VM B static answer through Gatherlink"

step "Counters And Diagnostics"
remote_a "status-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status dns.vm.node-a" | tee "${OUT_DIR}/status-node-a.json" >/dev/null
remote_b "status-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status dns.vm.node-b" | tee "${OUT_DIR}/status-node-b.json" >/dev/null
remote_a "copy-helper-log-a" "cat /tmp/dns-helper.log" >"${OUT_DIR}/dns-helper.log"
remote_b "copy-static-dns-log-b" "cat /tmp/dns-static-server.log" >"${OUT_DIR}/dns-static-server.log"
remote_a "copy-node-a-diag" "cat /tmp/dns-node-a.jsonl" >"${OUT_DIR}/dns-node-a.jsonl"
remote_b "copy-node-b-diag" "cat /tmp/dns-node-b.jsonl" >"${OUT_DIR}/dns-node-b.jsonl"
grep -q 'service.bound' "${OUT_DIR}/dns-node-a.jsonl"
grep -q 'service.bound' "${OUT_DIR}/dns-node-b.jsonl"
record "core status and diagnostics captured"

step "Result"
record "PASS: DNS helper tunnel upstream traversed Gatherlink and resolved from VM B"
echo "Hyper-V DNS acceptance passed. Report: ${REPORT}"
