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
KEEP_RUNNING=0
INVENTORY=""
THROUGHPUT_SECONDS=0
THROUGHPUT_TARGET_MBIT=0
THROUGHPUT_PAYLOAD_SIZE=65536
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
  --port-a PORT        Optional SSH port for VM A when using a shared portproxy IP.
  --port-b PORT        Optional SSH port for VM B when using a shared portproxy IP.
  --host-key-a KEY     PuTTY host-key fingerprint for VM A.
  --host-key-b KEY     PuTTY host-key fingerprint for VM B.
  --transport NAME     SSH transport: plink or ssh. Default plink.
  --branch NAME        Branch to push. Defaults current branch.
  --out DIR            Report directory.
  --build              Reinstall Python deps and rebuild PyO3 binding on VMs.
  --keep-running       Leave services/helpers running after the probe for debugging.
  --throughput-seconds N
                       Also run a TCP-forward throughput probe for N seconds.
  --throughput-target-mbit N
                       Optional TCP sender cap for the throughput probe. Default unbounded.
  --throughput-payload-size BYTES
                       TCP write block size for the throughput probe. Default 65536.
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
    --keep-running) KEEP_RUNNING=1; shift ;;
    --throughput-seconds) THROUGHPUT_SECONDS="$2"; shift 2 ;;
    --throughput-target-mbit) THROUGHPUT_TARGET_MBIT="$2"; shift 2 ;;
    --throughput-payload-size) THROUGHPUT_PAYLOAD_SIZE="$2"; shift 2 ;;
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
  remote_a "cleanup-a" "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close socks5.vm.node-a >/dev/null 2>&1 || true); pkill -f '[h]elpers socks5-serve' || true; pkill -f '[h]elpers tcp-forward' || true"
  remote_b "cleanup-b" "cd /home/gatherlink/src/gatherlink && (.venv/bin/gatherlink services close socks5.vm.node-b >/dev/null 2>&1 || true); pkill -f '[h]elpers stream-exit' || true; pkill -f '[h]elpers status-http' || true"
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
sync_node "node-a" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}"
sync_node "node-b" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}"
record "source synced by Git to both VMs"

step "Prepare Configs"
remote_a "write-node-a-config" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path('configs/hyperv/two-vm-node-a.json').read_text())
base = cfg['services'][0]
base['name'] = 'stream-socks5'
base['return_mode'] = 'learned-single-source'
tcp_forward = dict(base)
tcp_forward['name'] = 'stream-tcp-forward'
tcp_forward['listen'] = '127.0.0.1:55181'
cfg['services'] = [base, tcp_forward]
Path('/tmp/socks5-node-a.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"
remote_b "write-node-b-config" "cd /home/gatherlink/src/gatherlink && .venv/bin/python - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path('configs/hyperv/two-vm-node-b.json').read_text())
base = cfg['services'][0]
base['name'] = 'stream-socks5'
base['listen'] = '127.0.0.1:55190'
tcp_forward = dict(base)
tcp_forward['name'] = 'stream-tcp-forward'
tcp_forward['listen'] = '127.0.0.1:55191'
cfg['services'] = [base, tcp_forward]
Path('/tmp/socks5-node-b.json').write_text(json.dumps(cfg, indent=2, sort_keys=True))
PY"
remote_a "validate-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/socks5-node-a.json"
remote_b "validate-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/socks5-node-b.json"
record "generated VM configs validate; SOCKS5 and TCP forward use separate Gatherlink service ports"

step "Start"
cleanup
remote_b "start-status-http" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers status-http --listen 127.0.0.1:18081 --write-window-seconds 0 >/tmp/socks5-status-http.log 2>&1 </dev/null & echo \$! >/tmp/socks5-status-http.pid)"
remote_b "start-stream-exit" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers stream-exit --listen 127.0.0.1:51820 --allow-host 127.0.0.1 --allow-port 18081 --allow-port 18100 --diagnostics-jsonl /tmp/socks5-stream-exit.jsonl >/tmp/socks5-stream-exit.log 2>&1 </dev/null & echo \$! >/tmp/socks5-stream-exit.pid)"
remote_b "start-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/socks5-node-b.json --name socks5.vm.node-b --diagnostics-jsonl /tmp/socks5-node-b.jsonl"
sleep 1
remote_a "start-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start /tmp/socks5-node-a.json --name socks5.vm.node-a --diagnostics-jsonl /tmp/socks5-node-a.jsonl"
sleep 2
remote_b "status-node-b-started" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status socks5.vm.node-b >/tmp/socks5-node-b-start-status.json"
remote_a "status-node-a-started" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status socks5.vm.node-a >/tmp/socks5-node-a-start-status.json"
remote_a "start-socks5" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers socks5-serve --listen 127.0.0.1:1081 --allow-host 127.0.0.1 --allow-port 18081 --gatherlink-service 127.0.0.1:55180 --diagnostics-jsonl /tmp/socks5-helper.jsonl >/tmp/socks5-helper.log 2>&1 </dev/null & echo \$! >/tmp/socks5-helper.pid)"
remote_a "start-tcp-forward" "cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers tcp-forward --listen 127.0.0.1:18082 --target 127.0.0.1:18081 --gatherlink-service 127.0.0.1:55181 --diagnostics-jsonl /tmp/tcp-forward-helper.jsonl >/tmp/tcp-forward-helper.log 2>&1 </dev/null & echo \$! >/tmp/tcp-forward-helper.pid)"
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

if [[ "${THROUGHPUT_SECONDS}" != "0" ]]; then
  step "TCP Forward Throughput Probe"
  remote_b "start-tcp-throughput-sink" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/tcp-forward-speed-sink.json /tmp/tcp-forward-speed-sink.err /tmp/tcp-forward-speed-sink.pid && (nohup .venv/bin/python tools/tcp_stream_speed.py sink --bind 127.0.0.1:18100 --duration $((THROUGHPUT_SECONDS + 5)) --idle-after-first 2 >/tmp/tcp-forward-speed-sink.json 2>/tmp/tcp-forward-speed-sink.err </dev/null & echo \$! >/tmp/tcp-forward-speed-sink.pid)"
  remote_a "restart-tcp-forward-for-throughput" "if [ -f /tmp/tcp-forward-helper.pid ]; then kill \$(cat /tmp/tcp-forward-helper.pid) 2>/dev/null || true; fi; cd /home/gatherlink/src/gatherlink && (nohup .venv/bin/gatherlink helpers tcp-forward --listen 127.0.0.1:18083 --target 127.0.0.1:18100 --gatherlink-service 127.0.0.1:55181 --diagnostics-jsonl /tmp/tcp-forward-speed-helper.jsonl >/tmp/tcp-forward-speed-helper.log 2>&1 </dev/null & echo \$! >/tmp/tcp-forward-speed-helper.pid)"
  sleep 1
  remote_a "tcp-forward-throughput-send" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/tcp_stream_speed.py send --target 127.0.0.1:18083 --duration ${THROUGHPUT_SECONDS} --payload-size ${THROUGHPUT_PAYLOAD_SIZE} --target-mbit ${THROUGHPUT_TARGET_MBIT} > /tmp/tcp-forward-speed-sender.json"
  remote_b "wait-tcp-throughput-sink" "if [ -f /tmp/tcp-forward-speed-sink.pid ]; then for _ in \$(seq 1 $((THROUGHPUT_SECONDS + 15))); do kill -0 \$(cat /tmp/tcp-forward-speed-sink.pid) 2>/dev/null || exit 0; sleep 1; done; exit 1; fi"
  remote_a "copy-tcp-throughput-sender" "cat /tmp/tcp-forward-speed-sender.json" >"${OUT_DIR}/tcp-forward-throughput-sender.json"
  remote_b "copy-tcp-throughput-sink" "cat /tmp/tcp-forward-speed-sink.json" >"${OUT_DIR}/tcp-forward-throughput-sink.json"
  remote_a "copy-tcp-throughput-helper-diag" "cat /tmp/tcp-forward-speed-helper.jsonl" >"${OUT_DIR}/tcp-forward-throughput-helper.jsonl"
  python3 - "${OUT_DIR}/tcp-forward-throughput-sender.json" "${OUT_DIR}/tcp-forward-throughput-sink.json" <<'PY' | tee -a "${REPORT}"
import json
import sys
from pathlib import Path

sender = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sink = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
delta = int(sender.get("bytes", 0)) - int(sink.get("bytes", 0))
print(f"- sender_mbit: {float(sender.get('mbit_per_second', 0)):.2f}")
print(f"- sink_mbit: {float(sink.get('mbit_per_second', 0)):.2f}")
print(f"- sink_active_mbit: {float(sink.get('active_mbit_per_second', sink.get('mbit_per_second', 0))):.2f}")
print(f"- byte_delta: {delta}")
if delta < 0:
    raise SystemExit("sink reported more bytes than sender")
PY
  grep -q 'helper.stream.opened' "${OUT_DIR}/tcp-forward-throughput-helper.jsonl"
  record "TCP forward throughput probe traversed Gatherlink and emitted stream diagnostics"
fi

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
