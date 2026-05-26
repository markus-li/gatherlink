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
PORT_A=""
PORT_B=""
HOST_KEY_A=""
HOST_KEY_B=""
COUNT=5
DURATION=8
INTERVAL=0.01
PAYLOAD_SIZE=256
BUILD_RUST=1
KEEP_RUNNING=0
SHAPE_PROFILE="asymmetric"
MIN_DELIVERY_RATIO="0.90"
SCHEDULER_REAPPLY_INTERVAL=""
INVENTORY=""
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-vm-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage: run_gatherlink_vm_acceptance.sh --host-key-a KEY --host-key-b KEY [options]

Runs the Hyper-V two-VM Gatherlink acceptance path mostly from WSL/Bash. PowerShell
is used only as an optional Hyper-V-specific VM IP resolver when --ip-a/--ip-b
are omitted.

Options:
  --inventory FILE          Optional ignored env file with VM IPs, host keys, and defaults.
  --ip-a IP                 VM A management IP. If omitted, resolve via Hyper-V helper.
  --ip-b IP                 VM B management IP. If omitted, resolve via Hyper-V helper.
  --port-a PORT             Optional SSH port for VM A when using a shared portproxy IP.
  --port-b PORT             Optional SSH port for VM B when using a shared portproxy IP.
  --host-key-a KEY          PuTTY host-key fingerprint for VM A.
  --host-key-b KEY          PuTTY host-key fingerprint for VM B.
  --branch NAME             Branch to push to the VM-local bare repos. Defaults current branch.
  --count N                 Exact packet smoke count. Default 5.
  --duration SECONDS        Duration traffic run length. Default 8.
  --soak SECONDS            Alias for --duration, intended for longer operator runs.
  --interval SECONDS        Delay between duration packets. Default 0.01.
  --payload-size BYTES      Duration packet payload size. Default 256.
  --shape-profile NAME      clean, asymmetric, lossy, latency, or none. Default asymmetric.
  --min-delivery-ratio N    Minimum duration receive/send ratio. Default 0.90.
  --scheduler-reapply-interval SECONDS
                            Enable Python-owned live scheduler reapply at this cadence.
  --out DIR                 Acceptance report directory.
  --skip-build              Sync source but skip pip/maturin install.
  --keep-running            Leave services running after the run.
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
  COUNT="${HYPERV_ACCEPTANCE_COUNT:-${COUNT}}"
  DURATION="${HYPERV_ACCEPTANCE_DURATION:-${DURATION}}"
  INTERVAL="${HYPERV_ACCEPTANCE_INTERVAL:-${INTERVAL}}"
  PAYLOAD_SIZE="${HYPERV_ACCEPTANCE_PAYLOAD_SIZE:-${PAYLOAD_SIZE}}"
  SHAPE_PROFILE="${HYPERV_ACCEPTANCE_SHAPE_PROFILE:-${SHAPE_PROFILE}}"
  MIN_DELIVERY_RATIO="${HYPERV_ACCEPTANCE_MIN_DELIVERY_RATIO:-${MIN_DELIVERY_RATIO}}"
  SCHEDULER_REAPPLY_INTERVAL="${HYPERV_ACCEPTANCE_SCHEDULER_REAPPLY_INTERVAL:-${SCHEDULER_REAPPLY_INTERVAL}}"
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
    --branch) BRANCH="$2"; shift 2 ;;
    --count) COUNT="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --soak) DURATION="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --payload-size) PAYLOAD_SIZE="$2"; shift 2 ;;
    --shape-profile) SHAPE_PROFILE="$2"; shift 2 ;;
    --min-delivery-ratio) MIN_DELIVERY_RATIO="$2"; shift 2 ;;
    --scheduler-reapply-interval) SCHEDULER_REAPPLY_INTERVAL="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --skip-build) BUILD_RUST=0; shift ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "${HOST_KEY_A}" ]] || { echo "--host-key-a is required" >&2; exit 2; }
[[ -n "${HOST_KEY_B}" ]] || { echo "--host-key-b is required" >&2; exit 2; }
[[ "${HOST_KEY_A}" != \<* ]] || { echo "--host-key-a still looks like a placeholder" >&2; exit 2; }
[[ "${HOST_KEY_B}" != \<* ]] || { echo "--host-key-b still looks like a placeholder" >&2; exit 2; }
[[ -x "${PLINK}" ]] || { echo "plink not found at ${PLINK}" >&2; exit 2; }
case "${SHAPE_PROFILE}" in
  clean|asymmetric|lossy|latency|none) ;;
  *) echo "--shape-profile must be clean, asymmetric, lossy, latency, or none" >&2; exit 2 ;;
esac
if [[ -n "${SCHEDULER_REAPPLY_INTERVAL}" ]]; then
  python3 - "${SCHEDULER_REAPPLY_INTERVAL}" <<'PY'
import sys

try:
    value = float(sys.argv[1])
except ValueError as exc:
    raise SystemExit("--scheduler-reapply-interval must be numeric") from exc
if value <= 0:
    raise SystemExit("--scheduler-reapply-interval must be greater than zero")
PY
fi

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
  if [[ -n "${port}" ]]; then
    port_args=(-P "${port}")
  fi
  log_cmd "${label}" "plink ${ip}${port:+:${port}} ${command}"
  "${PLINK}" -batch -agent -hostkey "${host_key}" "${port_args[@]}" -l gatherlink "${ip}" "${command}"
}

remote_capture() {
  local label="$1"
  local ip="$2"
  local port="$3"
  local host_key="$4"
  local command="$5"
  local output="$6"
  local port_args=()
  if [[ -n "${port}" ]]; then
    port_args=(-P "${port}")
  fi
  log_cmd "${label}" "plink ${ip}${port:+:${port}} ${command} > ${output}"
  "${PLINK}" -batch -agent -hostkey "${host_key}" "${port_args[@]}" -l gatherlink "${ip}" "${command}" >"${output}"
}

remote_a() {
  remote "$1" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "$2"
}

remote_b() {
  remote "$1" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "$2"
}

sync_node() {
  local label="$1"
  local ip="$2"
  local port="$3"
  local host_key="$4"
  local git_port_args=""
  if [[ -n "${port}" ]]; then
    git_port_args=" -P ${port}"
  fi
  remote "${label}-prepare-repo" "${ip}" "${port}" "${host_key}" \
    "mkdir -p /home/gatherlink/repos && if [ ! -d /home/gatherlink/repos/gatherlink.git ]; then git init --bare /home/gatherlink/repos/gatherlink.git; fi && git --git-dir=/home/gatherlink/repos/gatherlink.git symbolic-ref HEAD refs/heads/${BRANCH} || true"
  log_cmd "${label}-push" "git push ssh://gatherlink@${ip}${port:+:${port}}/home/gatherlink/repos/gatherlink.git HEAD:${BRANCH}"
  (
    cd "${REPO_ROOT}"
    GIT_SSH_COMMAND="${PLINK} -batch -agent -hostkey ${host_key}${git_port_args}" \
      git push --force "ssh://gatherlink@${ip}/home/gatherlink/repos/gatherlink.git" "HEAD:refs/heads/${BRANCH}"
  )
  local install_command=""
  if [[ "${BUILD_RUST}" -eq 1 ]]; then
    install_command=" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt >/tmp/gatherlink-pip-install.log && .venv/bin/pip install -e . >>/tmp/gatherlink-pip-install.log && .venv/bin/maturin develop --manifest-path crates/pybindings/Cargo.toml --release >/tmp/gatherlink-maturin.log"
  fi
  remote "${label}-checkout" "${ip}" "${port}" "${host_key}" \
    "mkdir -p /home/gatherlink/src && if [ ! -d /home/gatherlink/src/gatherlink/.git ]; then rm -rf /home/gatherlink/src/gatherlink && git clone /home/gatherlink/repos/gatherlink.git /home/gatherlink/src/gatherlink; fi && cd /home/gatherlink/src/gatherlink && git fetch origin && git reset --hard origin/${BRANCH}${install_command}"
}

cleanup_services_and_shapes() {
  # The Hyper-V acceptance VMs are dedicated lab machines. Close any
  # process-managed Gatherlink service before binding the v0.9 node ports so
  # helper demos from a previous run cannot hold UDP sockets and create a false
  # dataplane failure.
  remote_a "cleanup-node-a" "cd /home/gatherlink/src/gatherlink && if [ -x .venv/bin/gatherlink ]; then .venv/bin/gatherlink services list | awk '/^[^[:space:]:]+[[:space:]]/ && \$0 !~ / manager=remote / {print \$1}' | while read -r service; do [ -n \"\${service}\" ] && .venv/bin/gatherlink services close \"\${service}\" || true; done; fi; for path in path-a path-b path-c; do sudo tc qdisc del dev \${path} root 2>/dev/null || true; sudo ip link set \${path} up; done"
  remote_b "cleanup-node-b" "cd /home/gatherlink/src/gatherlink && if [ -x .venv/bin/gatherlink ]; then .venv/bin/gatherlink services list | awk '/^[^[:space:]:]+[[:space:]]/ && \$0 !~ / manager=remote / {print \$1}' | while read -r service; do [ -n \"\${service}\" ] && .venv/bin/gatherlink services close \"\${service}\" || true; done; fi; for path in path-a path-b path-c; do sudo tc qdisc del dev \${path} root 2>/dev/null || true; sudo ip link set \${path} up; done"
}

start_services() {
  local scheduler_args=""
  if [[ -n "${SCHEDULER_REAPPLY_INTERVAL}" ]]; then
    scheduler_args=" --scheduler-reapply-interval ${SCHEDULER_REAPPLY_INTERVAL}"
  fi
  remote_b "start-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start configs/hyperv/two-vm-node-b.json --name vm.node-b --diagnostics-jsonl /tmp/gatherlink-node-b.jsonl${scheduler_args}"
  sleep 1
  remote_a "start-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink run start configs/hyperv/two-vm-node-a.json --name vm.node-a --diagnostics-jsonl /tmp/gatherlink-node-a.jsonl${scheduler_args}"
  sleep 2
}

stop_services() {
  remote_a "stop-node-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close vm.node-a || true"
  remote_b "stop-node-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close vm.node-b || true"
}

run_packet_smoke() {
  local label="$1"
  local count="$2"
  local payload="$3"
  remote_b "${label}-receiver" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/${label}-received.txt; (timeout 30 .venv/bin/python tools/udp_probe.py receive 127.0.0.1:51820 --count ${count} > /tmp/${label}-received.txt 2>&1 & echo \$! > /tmp/${label}-receiver.pid)"
  sleep 1
  remote_a "${label}-send" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/udp_probe.py send 127.0.0.1:55180 ${payload} --count ${count}"
  sleep 2
  remote_b "${label}-verify" "test \$(grep -c '^${payload}' /tmp/${label}-received.txt) -eq ${count} && cat /tmp/${label}-received.txt"
}

run_duration_traffic() {
  remote_b "duration-receiver" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/duration-received.txt /tmp/duration-received-count.txt; (timeout $((DURATION + 8)) .venv/bin/python tools/udp_probe.py receive 127.0.0.1:51820 --count 10000000 --min-count 1 --timeout 3 --max-print-packets 0 --count-file /tmp/duration-received-count.txt > /tmp/duration-received.txt 2>&1 & echo \$! > /tmp/duration-receiver.pid)"
  sleep 1
  remote_a "duration-send" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/udp_probe.py send 127.0.0.1:55180 hyperv-duration --count 0 --duration ${DURATION} --interval ${INTERVAL} --payload-size ${PAYLOAD_SIZE} > /tmp/duration-sent.txt"
  # The receive probe writes its count file periodically during the stream and
  # once more when its idle timeout expires. Wait for that receiver process so
  # the acceptance ratio is based on the final count, not a rounded in-flight
  # snapshot.
  remote_b "duration-wait-receiver" "pid=\$(cat /tmp/duration-receiver.pid); while kill -0 \"\${pid}\" 2>/dev/null; do sleep 0.2; done"
  remote_capture "duration-sent-count" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "grep '^sent_packets=' /tmp/duration-sent.txt" "${OUT_DIR}/duration-sent.txt"
  remote_capture "duration-received-count" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "cat /tmp/duration-received-count.txt" "${OUT_DIR}/duration-received.txt"
  assert_duration_delivery
}

capture_status() {
  local phase="$1"
  remote_a "status-a-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status vm.node-a" | tee "${OUT_DIR}/status-node-a-${phase}.json" >/dev/null
  remote_b "status-b-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status vm.node-b" | tee "${OUT_DIR}/status-node-b-${phase}.json" >/dev/null
  remote_a "monitor-a-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor vm.node-a --once" | tee "${OUT_DIR}/monitor-node-a-${phase}.txt" >/dev/null
  remote_b "monitor-b-${phase}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor vm.node-b --once" | tee "${OUT_DIR}/monitor-node-b-${phase}.txt" >/dev/null
  summarize_status "${phase}"
}

summarize_status() {
  local phase="$1"
  python3 - "${REPORT}" "${phase}" "${OUT_DIR}/status-node-a-${phase}.json" "${OUT_DIR}/status-node-b-${phase}.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

report = Path(sys.argv[1])
phase = sys.argv[2]
status_files = [Path(path) for path in sys.argv[3:]]

with report.open("a", encoding="utf-8") as handle:
    handle.write(f"\n### Path Split: {phase}\n\n")
    handle.write("| node | path | tx_packets | rx_packets | tx_bytes | rx_bytes | missed | ooo | reord |\n")
    handle.write("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n")
    for status_file in status_files:
        status = json.loads(status_file.read_text(encoding="utf-8"))
        node = str(status.get("node", status_file.stem))
        for path_name, stats in sorted((status.get("path_stats") or {}).items()):
            handle.write(
                f"| {node} | {path_name} | "
                f"{int(stats.get('tx_packets', 0))} | {int(stats.get('rx_packets', 0))} | "
                f"{int(stats.get('tx_bytes', 0))} | {int(stats.get('rx_bytes', 0))} | "
                f"{int(stats.get('missed_packets', 0))} | {int(stats.get('packets_needing_reorder', 0))} | "
                f"{int(stats.get('reordered_packets', 0))} |\n"
            )
        handle.write(
            f"| {node} | total | {int(status.get('tx_packets', 0))} | {int(status.get('rx_packets', 0))} | "
            f"{int(status.get('tx_bytes', 0))} | {int(status.get('rx_bytes', 0))} |  |  |  |\n"
        )
PY
}

shape_profile() {
  case "${SHAPE_PROFILE}" in
    none)
      record "shape profile none: leaving current path shaping untouched"
      ;;
    clean)
      clear_shapes
      record "shape profile clean: all lab qdiscs cleared"
      ;;
    asymmetric)
      remote_a "shape-a-asymmetric" "sudo tc qdisc replace dev path-a root netem rate 3mbit delay 5ms; sudo tc qdisc replace dev path-b root netem rate 2mbit delay 15ms loss 0.2%; sudo tc qdisc replace dev path-c root netem rate 1mbit delay 30ms"
      remote_b "shape-b-asymmetric" "sudo tc qdisc replace dev path-a root netem rate 2mbit delay 7ms; sudo tc qdisc replace dev path-b root netem rate 1500kbit delay 18ms loss 0.2%; sudo tc qdisc replace dev path-c root netem rate 1200kbit delay 35ms"
      ;;
    lossy)
      remote_a "shape-a-lossy" "sudo tc qdisc replace dev path-a root netem rate 3mbit loss 0.5%; sudo tc qdisc replace dev path-b root netem rate 2mbit loss 1%; sudo tc qdisc replace dev path-c root netem rate 1mbit loss 2%"
      remote_b "shape-b-lossy" "sudo tc qdisc replace dev path-a root netem rate 3mbit loss 0.5%; sudo tc qdisc replace dev path-b root netem rate 2mbit loss 1%; sudo tc qdisc replace dev path-c root netem rate 1mbit loss 2%"
      ;;
    latency)
      remote_a "shape-a-latency" "sudo tc qdisc replace dev path-a root netem delay 5ms; sudo tc qdisc replace dev path-b root netem delay 30ms 5ms; sudo tc qdisc replace dev path-c root netem delay 80ms 15ms"
      remote_b "shape-b-latency" "sudo tc qdisc replace dev path-a root netem delay 5ms; sudo tc qdisc replace dev path-b root netem delay 30ms 5ms; sudo tc qdisc replace dev path-c root netem delay 80ms 15ms"
      ;;
  esac
}

clear_shapes() {
  remote_a "clear-shapes-a" "for path in path-a path-b path-c; do sudo tc qdisc del dev \${path} root 2>/dev/null || true; sudo ip link set \${path} up; done"
  remote_b "clear-shapes-b" "for path in path-a path-b path-c; do sudo tc qdisc del dev \${path} root 2>/dev/null || true; sudo ip link set \${path} up; done"
}

flap_each_path() {
  for path in path-a path-b path-c; do
    step "Fail And Recover ${path}"
    remote_a "down-${path}" "sudo ip link set ${path} down"
    sleep 1
    remote_a "up-${path}" "sudo ip link set ${path} up"
    sleep 2
    run_packet_smoke "recover-${path}" "${COUNT}" "recover-${path}"
    capture_status "recover-${path}"
    record "${path} recovered and carried UDP after link up"
  done
}

assert_duration_delivery() {
  python3 - "${OUT_DIR}/duration-sent.txt" "${OUT_DIR}/duration-received.txt" "${MIN_DELIVERY_RATIO}" "${REPORT}" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

sent_path = Path(sys.argv[1])
received_path = Path(sys.argv[2])
minimum = float(sys.argv[3])
report = Path(sys.argv[4])

def read_value(path: Path, prefix: str) -> int:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return int(line.split("=", 1)[1])
    raise SystemExit(f"{path} did not contain {prefix}")

sent = read_value(sent_path, "sent_packets=")
received = read_value(received_path, "received_packets=")
ratio = received / sent if sent else 0.0
with report.open("a", encoding="utf-8") as handle:
    handle.write(f"\n- duration sent packets: {sent}\n")
    handle.write(f"- duration received packets: {received}\n")
    handle.write(f"- duration delivery ratio: {ratio:.4f} (minimum {minimum:.4f})\n")
if sent <= 0 or ratio < minimum:
    raise SystemExit(f"duration delivery ratio {ratio:.4f} is below minimum {minimum:.4f}")
PY
}

assert_path_split() {
  local phase="$1"
  python3 - "${phase}" "${OUT_DIR}/status-node-a-${phase}.json" "${OUT_DIR}/status-node-b-${phase}.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

phase = sys.argv[1]
paths = ("path-a", "path-b", "path-c")
source = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
sink = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
source_stats = source.get("path_stats") or {}
sink_stats = sink.get("path_stats") or {}
missing: list[str] = []
for path in paths:
    if int((source_stats.get(path) or {}).get("tx_packets", 0)) <= 0:
        missing.append(f"source {path} tx")
    if int((sink_stats.get(path) or {}).get("rx_packets", 0)) <= 0:
        missing.append(f"sink {path} rx")
if missing:
    raise SystemExit(f"{phase} missing expected path split: {', '.join(missing)}")
PY
}

assert_service_cleanup() {
  remote_capture "list-a-after-stop" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-node-a.txt"
  remote_capture "list-b-after-stop" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services list" "${OUT_DIR}/services-node-b.txt"
  if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
    return
  fi
  grep -q "vm.node-a .*state=stopped" "${OUT_DIR}/services-node-a.txt"
  grep -q "vm.node-b .*state=stopped" "${OUT_DIR}/services-node-b.txt"
  remote_capture "prune-a-after-stop" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services prune && .venv/bin/gatherlink services list" "${OUT_DIR}/services-node-a-pruned.txt"
  remote_capture "prune-b-after-stop" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services prune && .venv/bin/gatherlink services list" "${OUT_DIR}/services-node-b-pruned.txt"
  grep -q "services: none" "${OUT_DIR}/services-node-a-pruned.txt"
  grep -q "services: none" "${OUT_DIR}/services-node-b-pruned.txt"
}

assert_diagnostics_meaningful() {
  local expect_shutdown="$1"
  remote_capture "diag-a-copy" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "cat /tmp/gatherlink-node-a.jsonl 2>/dev/null || true" "${OUT_DIR}/diagnostics-node-a.jsonl"
  remote_capture "diag-b-copy" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "cat /tmp/gatherlink-node-b.jsonl 2>/dev/null || true" "${OUT_DIR}/diagnostics-node-b.jsonl"
  python3 "${REPO_ROOT}/tools/vm_acceptance/validate_jsonl.py" "${OUT_DIR}/diagnostics-node-a.jsonl" | tee "${OUT_DIR}/diagnostics-node-a.validation.txt"
  python3 "${REPO_ROOT}/tools/vm_acceptance/validate_jsonl.py" "${OUT_DIR}/diagnostics-node-b.jsonl" | tee "${OUT_DIR}/diagnostics-node-b.validation.txt"
  python3 - "${expect_shutdown}" "${SCHEDULER_REAPPLY_INTERVAL}" "${OUT_DIR}/diagnostics-node-a.jsonl" "${OUT_DIR}/diagnostics-node-b.jsonl" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

expect_shutdown = sys.argv[1] == "yes"
scheduler_reapply_enabled = bool(sys.argv[2])
required = {"service.bound", "counter.snapshot"}
if expect_shutdown:
    required.add("runtime.shutdown")
if scheduler_reapply_enabled:
    required.update({"scheduler.decision", "config.reapplied"})
for raw_path in sys.argv[3:]:
    path = Path(raw_path)
    codes = {
        json.loads(line).get("code")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    missing = sorted(required - codes)
    if missing:
        raise SystemExit(f"{path} missing diagnostics events: {', '.join(missing)}")
PY
}

cat >"${REPORT}" <<REPORT
# Hyper-V Two-VM Gatherlink Acceptance

- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- branch: ${BRANCH}
- vm_a: ${VM_A} ${IP_A}
- vm_b: ${VM_B} ${IP_B}
- count: ${COUNT}
- duration_seconds: ${DURATION}
- interval_seconds: ${INTERVAL}
- payload_size: ${PAYLOAD_SIZE}
- shape_profile: ${SHAPE_PROFILE}
- min_delivery_ratio: ${MIN_DELIVERY_RATIO}
- scheduler_reapply_interval: ${SCHEDULER_REAPPLY_INTERVAL:-disabled}

REPORT

step "Sync And Build"
sync_node "node-a" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}"
sync_node "node-b" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}"
record "source synced by Git and VM working trees reset to ${BRANCH}"

step "Validate"
remote_a "validate-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate configs/hyperv/two-vm-node-a.json"
remote_b "validate-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate configs/hyperv/two-vm-node-b.json"
record "both Hyper-V configs validate on the VMs"

step "Start"
cleanup_services_and_shapes
start_services
if [[ -n "${SCHEDULER_REAPPLY_INTERVAL}" ]]; then
  record "both managed services started with live scheduler reapply every ${SCHEDULER_REAPPLY_INTERVAL}s"
else
  record "both managed services started"
fi

step "Exact Packet Smoke"
run_packet_smoke "exact" "${COUNT}" "hyperv-exact"
capture_status "exact"
record "exact packet smoke delivered ${COUNT}/${COUNT} packets"

step "Shaping And Duration Traffic"
shape_profile
run_duration_traffic
capture_status "duration-shaped"
assert_path_split "duration-shaped"
record "duration traffic delivered over shape profile ${SHAPE_PROFILE}"
clear_shapes

flap_each_path

if [[ "${KEEP_RUNNING}" -eq 0 ]]; then
  step "Stop"
  stop_services
  assert_service_cleanup
  record "managed services closed"
else
  step "Left Running"
  assert_service_cleanup
  record "services intentionally left running for manual inspection"
fi

step "Post Run"
assert_diagnostics_meaningful "$([[ "${KEEP_RUNNING}" -eq 0 ]] && echo yes || echo no)"
record "service listings captured"
record "stopped process-managed service records pruned when services were not left running"
if [[ -n "${SCHEDULER_REAPPLY_INTERVAL}" ]]; then
  record "scheduler decision and config reapply diagnostics observed"
fi
record "diagnostics JSONL copied, parsed, and checked for lifecycle/counter events"
record "command log: ${COMMAND_LOG}"

printf '\nAcceptance complete.\nReport: %s\nCommands: %s\n' "${REPORT}" "${COMMAND_LOG}"
