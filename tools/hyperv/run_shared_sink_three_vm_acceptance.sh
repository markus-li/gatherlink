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
REQUESTS_PER_SOURCE=3
KEEP_RUNNING=0
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-three-vm-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage: run_shared_sink_three_vm_acceptance.sh --host-key-a KEY --host-key-b KEY --host-key-c KEY [options]

Runs the Hyper-V three-VM shared-sink proof:
  VM A -> VM B shared sink
  VM C -> VM B shared sink

VM B listens on one Gatherlink carrier port per path and distinguishes the two
authenticated sources by session/receiver-index state, not by separate sink
ports.

Options:
  --ip-a IP                 VM A management IP. If omitted, resolve from ARP.
  --ip-b IP                 VM B management IP. If omitted, resolve from ARP.
  --ip-c IP                 VM C management IP. If omitted, resolve from ARP.
  --host-key-a KEY          PuTTY host-key fingerprint for VM A.
  --host-key-b KEY          PuTTY host-key fingerprint for VM B.
  --host-key-c KEY          PuTTY host-key fingerprint for VM C.
  --branch NAME             Branch to push to the VM-local bare repos. Defaults current branch.
  --requests-per-source N   UDP request/reply probes per source. Default 3.
  --out DIR                 Acceptance report directory.
  --skip-build              Sync source but skip pip/maturin install.
  --keep-running            Leave the three core services running for operator inspection.
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
    --requests-per-source) REQUESTS_PER_SOURCE="$2"; shift 2 ;;
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
[[ "${REQUESTS_PER_SOURCE}" =~ ^[0-9]+$ ]] || { echo "--requests-per-source must be numeric" >&2; exit 2; }
[[ "${REQUESTS_PER_SOURCE}" -gt 0 ]] || { echo "--requests-per-source must be positive" >&2; exit 2; }

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
    "cd /home/gatherlink/src/gatherlink && if [ -x .venv/bin/gatherlink ]; then .venv/bin/gatherlink services list | awk '/^[^[:space:]:]+[[:space:]]/ && \$0 !~ /kind=remote/ {print \$1}' | while read -r service; do [ -n \"\${service}\" ] && .venv/bin/gatherlink services close \"\${service}\" || true; done; fi; for path in path-a path-b path-c; do sudo tc qdisc del dev \${path} root 2>/dev/null || true; sudo ip link set \${path} up; done"
}

capture_status() {
  local phase="$1"
  remote_capture_a "status-a-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status vm.source-a" "${OUT_DIR}/status-source-a-${phase}.json"
  remote_capture_b "status-b-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status vm.shared-sink" "${OUT_DIR}/status-shared-sink-${phase}.json"
  remote_capture_c "status-c-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status vm.source-c" "${OUT_DIR}/status-source-c-${phase}.json"
  remote_capture_a "monitor-a-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor vm.source-a --once" "${OUT_DIR}/monitor-source-a-${phase}.txt"
  remote_capture_b "monitor-b-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor vm.shared-sink --once" "${OUT_DIR}/monitor-shared-sink-${phase}.txt"
  remote_capture_c "monitor-c-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor vm.source-c --once" "${OUT_DIR}/monitor-source-c-${phase}.txt"
}

stop_services() {
  remote_a "stop-source-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close vm.source-a || true"
  remote_b "stop-shared-sink" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close vm.shared-sink || true"
  remote_c "stop-source-c" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close vm.source-c || true"
}

if [[ "${KEEP_RUNNING}" -eq 0 ]]; then
  trap 'stop_services >/dev/null 2>&1 || true' EXIT
fi

cat >"${REPORT}" <<EOF
# Hyper-V Three-VM Shared Sink Acceptance

- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- branch: ${BRANCH}
- vm_a: ${VM_A} ${IP_A}
- vm_b: ${VM_B} ${IP_B}
- vm_c: ${VM_C} ${IP_C}
- requests_per_source: ${REQUESTS_PER_SOURCE}
- keep_running: ${KEEP_RUNNING}

EOF

step "Sync And Build"
sync_node "source-a" "${IP_A}" "${HOST_KEY_A}"
sync_node "shared-sink" "${IP_B}" "${HOST_KEY_B}"
sync_node "source-c" "${IP_C}" "${HOST_KEY_C}"
record "source synced by Git and VM working trees reset to ${BRANCH}"

step "Validate"
remote_a "validate-source-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate configs/hyperv/three-vm-shared-sink-source-a.json"
remote_b "validate-shared-sink" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate configs/hyperv/three-vm-shared-sink-server.json"
remote_c "validate-source-c" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate configs/hyperv/three-vm-shared-sink-source-c.json"
record "three shared-sink configs validate on their VMs"

step "Start"
cleanup_node "source-a" "${IP_A}" "${HOST_KEY_A}"
cleanup_node "shared-sink" "${IP_B}" "${HOST_KEY_B}"
cleanup_node "source-c" "${IP_C}" "${HOST_KEY_C}"
remote_b "start-shared-sink" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start configs/hyperv/three-vm-shared-sink-server.json --name vm.shared-sink --diagnostics-jsonl /tmp/three-vm-shared-sink.jsonl"
sleep 1
remote_a "start-source-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start configs/hyperv/three-vm-shared-sink-source-a.json --name vm.source-a --diagnostics-jsonl /tmp/three-vm-source-a.jsonl"
remote_c "start-source-c" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start configs/hyperv/three-vm-shared-sink-source-c.json --name vm.source-c --diagnostics-jsonl /tmp/three-vm-source-c.jsonl"
sleep 2
record "shared sink and two sources started"

step "Shared Sink Request Reply"
total_requests=$((REQUESTS_PER_SOURCE * 2))
remote_b "start-echo" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/three-vm-echo.txt; (timeout 60 .venv/bin/python tools/udp_probe.py echo 127.0.0.1:51820 --count ${total_requests} --timeout 50 > /tmp/three-vm-echo.txt 2>&1 & echo \$! > /tmp/three-vm-echo.pid)"
sleep 1
remote_a "request-source-a" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/three-vm-source-a-requests.txt; for index in \$(seq 1 ${REQUESTS_PER_SOURCE}); do .venv/bin/python tools/udp_probe.py request 127.0.0.1:55180 source-a-\${index} --timeout 20 >>/tmp/three-vm-source-a-requests.txt; done; cat /tmp/three-vm-source-a-requests.txt"
remote_c "request-source-c" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/three-vm-source-c-requests.txt; for index in \$(seq 1 ${REQUESTS_PER_SOURCE}); do .venv/bin/python tools/udp_probe.py request 127.0.0.1:55180 source-c-\${index} --timeout 20 >>/tmp/three-vm-source-c-requests.txt; done; cat /tmp/three-vm-source-c-requests.txt"
sleep 2
remote_b "verify-echo" "test \$(grep -c '^source-a-' /tmp/three-vm-echo.txt) -eq ${REQUESTS_PER_SOURCE}; test \$(grep -c '^source-c-' /tmp/three-vm-echo.txt) -eq ${REQUESTS_PER_SOURCE}; cat /tmp/three-vm-echo.txt"
record "VM A and VM C both completed request/reply traffic through VM B's shared sink"

capture_status "request-reply"
record "status and monitor snapshots captured"

step "Operator Views On Shared Sink"
remote_capture_b "list-shared-sink-running" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-shared-sink-running.txt"
remote_capture_b "monitor-shared-sink-live" "cd /home/gatherlink/src/gatherlink && timeout 5 .venv/bin/gatherlink services monitor vm.shared-sink --interval 1 >/tmp/three-vm-shared-sink-live-monitor.txt 2>&1; status=\$?; if [ \${status} -ne 0 ] && [ \${status} -ne 124 ]; then exit \${status}; fi; cat /tmp/three-vm-shared-sink-live-monitor.txt" "${OUT_DIR}/monitor-shared-sink-live.txt"
grep -q "remote.vm.shared-sink" "${OUT_DIR}/services-shared-sink-running.txt"
grep -q "remote source-a" "${OUT_DIR}/monitor-shared-sink-live.txt"
grep -q "remote source-c" "${OUT_DIR}/monitor-shared-sink-live.txt"
record "services list and live monitor on VM B show learned remote service/status rows"

if [[ "${KEEP_RUNNING}" -eq 0 ]]; then
  step "Stop"
  stop_services
  record "three VM services closed"
else
  step "Left Running"
  record "three VM services intentionally left running for operator inspection"
fi

step "Post Run"
remote_capture_a "list-source-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-source-a.txt"
remote_capture_b "list-shared-sink" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-shared-sink.txt"
remote_capture_c "list-source-c" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-source-c.txt"
record "service listings captured after cleanup"
record "command log: ${COMMAND_LOG}"

printf '\nAcceptance complete.\nReport: %s\nCommands: %s\n' "${REPORT}" "${COMMAND_LOG}"
