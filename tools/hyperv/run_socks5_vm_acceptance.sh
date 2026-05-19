#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PLINK="${PLINK:-/mnt/c/Progra~1/PuTTY/plink.exe}"
BRANCH="$(cd "${REPO_ROOT}" && git rev-parse --abbrev-ref HEAD)"
VM_A="gatherlink-vm-a"
VM_B="gatherlink-vm-b"
IP_A=""
IP_B=""
HOST_KEY_A=""
HOST_KEY_B=""
BUILD_RUST=0
KEEP_RUNNING=0
INVENTORY=""
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-socks5-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage: run_socks5_vm_acceptance.sh --host-key-a KEY --host-key-b KEY [options]

Runs two VM helper-over-Gatherlink proofs:

SOCKS client on VM A -> SOCKS5 helper on VM A -> Gatherlink core A/B ->
stream-exit on VM B -> status HTTP helper on VM B.

HTTP client on VM A -> TCP forward helper on VM A -> Gatherlink core A/B ->
stream-exit on VM B -> status HTTP helper on VM B.

Options:
  --inventory FILE     Optional ignored env file with HYPERV_VM_* values.
  --ip-a IP            VM A management IP. If omitted, resolve via Hyper-V helper.
  --ip-b IP            VM B management IP. If omitted, resolve via Hyper-V helper.
  --host-key-a KEY     PuTTY host-key fingerprint for VM A.
  --host-key-b KEY     PuTTY host-key fingerprint for VM B.
  --branch NAME        Branch to push. Defaults current branch.
  --out DIR            Report directory.
  --build              Reinstall Python deps and rebuild PyO3 binding on VMs.
  --keep-running       Leave services/helpers running after the probe for debugging.
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
    --keep-running) KEEP_RUNNING=1; shift ;;
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

resolve_vm_ip() {
  local helper_windows
  helper_windows="$(wslpath -w "${SCRIPT_DIR}/resolve_gatherlink_vm.ps1")"
  powershell.exe -ExecutionPolicy Bypass -File "${helper_windows}" -Name "$1" | tr -d '\r'
}

if [[ -z "${IP_A}" ]]; then IP_A="$(resolve_vm_ip "${VM_A}")"; fi
if [[ -z "${IP_B}" ]]; then IP_B="$(resolve_vm_ip "${VM_B}")"; fi

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
  remote_a "cleanup-a" "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close socks5.vm.node-a || true); pkill -f '[h]elpers socks5-serve' || true; pkill -f '[h]elpers tcp-forward' || true"
  remote_b "cleanup-b" "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close socks5.vm.node-b || true); pkill -f '[h]elpers stream-exit' || true; pkill -f '[h]elpers status-http' || true"
}

trap cleanup EXIT
if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
  trap - EXIT
fi

cat >"${REPORT}" <<REPORT
# Hyper-V SOCKS5 Over Gatherlink Acceptance

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
cfg['services'][0]['return_mode'] = 'learned-single-source'
Path('/tmp/socks5-node-a.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"
remote_b "write-node-b-config" "cd /home/gatherlink/src/gatherlink && cp configs/hyperv/two-vm-node-b.json /tmp/socks5-node-b.json"
remote_a "validate-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/socks5-node-a.json"
remote_b "validate-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/socks5-node-b.json"
record "generated VM configs validate; node A uses learned-single-source replies for the SOCKS helper stream"

step "Start"
cleanup
remote_b "start-status-http" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers status-http --listen 127.0.0.1:18081 --write-window-seconds 0 >/tmp/socks5-status-http.log 2>&1 </dev/null & echo \$! >/tmp/socks5-status-http.pid)"
remote_b "start-stream-exit" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers stream-exit --listen 127.0.0.1:51820 --allow-host 127.0.0.1 --allow-port 18081 --diagnostics-jsonl /tmp/socks5-stream-exit.jsonl >/tmp/socks5-stream-exit.log 2>&1 </dev/null & echo \$! >/tmp/socks5-stream-exit.pid)"
remote_b "start-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/socks5-node-b.json --name socks5.vm.node-b --diagnostics-jsonl /tmp/socks5-node-b.jsonl"
sleep 1
remote_a "start-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/socks5-node-a.json --name socks5.vm.node-a --diagnostics-jsonl /tmp/socks5-node-a.jsonl"
sleep 2
remote_a "start-socks5" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers socks5-serve --listen 127.0.0.1:1081 --allow-host 127.0.0.1 --allow-port 18081 --gatherlink-service 127.0.0.1:55180 --diagnostics-jsonl /tmp/socks5-helper.jsonl >/tmp/socks5-helper.log 2>&1 </dev/null & echo \$! >/tmp/socks5-helper.pid)"
remote_a "start-tcp-forward" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers tcp-forward --listen 127.0.0.1:18082 --target 127.0.0.1:18081 --gatherlink-service 127.0.0.1:55180 --diagnostics-jsonl /tmp/tcp-forward-helper.jsonl >/tmp/tcp-forward-helper.log 2>&1 </dev/null & echo \$! >/tmp/tcp-forward-helper.pid)"
sleep 1
record "status HTTP, stream exit, both core services, SOCKS5 helper, and TCP forward helper started"

step "SOCKS5 HTTP Probe"
remote_a "probe" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/socks5_http_probe.py --socks 127.0.0.1:1081 --target 127.0.0.1:18081 --path /text --timeout 15 | tee /tmp/socks5-probe.txt"
remote_a "probe-verify" "grep -q 'Gatherlink local status (EXPERIMENTAL)' /tmp/socks5-probe.txt && grep -q 'socks5.vm.node-b' /tmp/socks5-probe.txt"
record "SOCKS5 CONNECT fetched the VM B status HTTP helper through Gatherlink"

step "TCP Forward HTTP Probe"
remote_a "tcp-forward-probe" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/http_probe.py --target 127.0.0.1:18082 --path /text --timeout 15 | tee /tmp/tcp-forward-probe.txt"
remote_a "tcp-forward-probe-verify" "grep -q 'Gatherlink local status (EXPERIMENTAL)' /tmp/tcp-forward-probe.txt && grep -q 'socks5.vm.node-b' /tmp/tcp-forward-probe.txt"
record "TCP forward fetched the VM B status HTTP helper through Gatherlink"

step "Counters And Diagnostics"
remote_a "status-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status socks5.vm.node-a" | tee "${OUT_DIR}/status-node-a.json" >/dev/null
remote_b "status-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status socks5.vm.node-b" | tee "${OUT_DIR}/status-node-b.json" >/dev/null
remote_a "copy-helper-diag-a" "cat /tmp/socks5-helper.jsonl" >"${OUT_DIR}/socks5-helper.jsonl"
remote_a "copy-tcp-helper-diag-a" "cat /tmp/tcp-forward-helper.jsonl" >"${OUT_DIR}/tcp-forward-helper.jsonl"
remote_b "copy-stream-diag-b" "cat /tmp/socks5-stream-exit.jsonl" >"${OUT_DIR}/socks5-stream-exit.jsonl"
grep -q 'helper.stream.opened' "${OUT_DIR}/socks5-helper.jsonl"
grep -q 'helper.stream.opened' "${OUT_DIR}/tcp-forward-helper.jsonl"
grep -q 'helper.stream.opened' "${OUT_DIR}/socks5-stream-exit.jsonl"
record "core status and helper stream diagnostics captured"

step "Result"
record "PASS: SOCKS5 and TCP forward helper traffic traversed Gatherlink and exited on VM B"
echo "Hyper-V stream helper acceptance passed. Report: ${REPORT}"
