#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/vm_ip_cache.sh"

PLINK="${PLINK:-/mnt/c/Progra~1/PuTTY/plink.exe}"
PSCP="${PSCP:-/mnt/c/Progra~1/PuTTY/pscp.exe}"
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
COUNT=80
POST_REKEY_COUNT=5
PAYLOAD_SIZE=256
BUILD_RUST=1
INVENTORY=""
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-rekey-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage: run_rekey_vm_acceptance.sh --host-key-a KEY --host-key-b KEY [options]

Proves autonomous authenticated live rekey on the two Hyper-V acceptance VMs.
The script provisions temporary Noise-authenticated security material with a
low packet rekey threshold, starts both managed services with explicit rekey
identity/topology inputs, sends live UDP traffic, and requires rekey diagnostics
plus post-rekey packet delivery.

Options:
  --inventory FILE          Optional ignored env file with VM IPs, SSH ports, host keys, and defaults.
  --ip-a IP                 VM A management IP. If omitted, resolve/cache through vm_ip_cache.sh.
  --ip-b IP                 VM B management IP. If omitted, resolve/cache through vm_ip_cache.sh.
  --port-a PORT             Optional SSH port for VM A when using a shared portproxy IP.
  --port-b PORT             Optional SSH port for VM B when using a shared portproxy IP.
  --host-key-a KEY          PuTTY host-key fingerprint for VM A.
  --host-key-b KEY          PuTTY host-key fingerprint for VM B.
  --transport NAME          SSH transport: plink or ssh. Default plink.
  --branch NAME             Branch to push to VM-local bare repos. Defaults current branch.
  --count N                 In-flight traffic packets used to trigger rekey. Default 80.
  --post-rekey-count N      Exact packet count sent after successful rekey. Default 5.
  --payload-size BYTES      In-flight packet payload size. Default 256.
  --out DIR                 Acceptance report directory.
  --skip-build              Sync source but skip pip/maturin install.
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
    --count) COUNT="$2"; shift 2 ;;
    --post-rekey-count) POST_REKEY_COUNT="$2"; shift 2 ;;
    --payload-size) PAYLOAD_SIZE="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --skip-build) BUILD_RUST=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "${TRANSPORT}" in
  plink)
    [[ -n "${HOST_KEY_A}" ]] || { echo "--host-key-a is required" >&2; exit 2; }
    [[ -n "${HOST_KEY_B}" ]] || { echo "--host-key-b is required" >&2; exit 2; }
    [[ -x "${PLINK}" ]] || { echo "plink not found at ${PLINK}" >&2; exit 2; }
    [[ -x "${PSCP}" ]] || { echo "pscp not found at ${PSCP}" >&2; exit 2; }
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

port_args() {
  local port="$1"
  if [[ -n "${port}" ]]; then
    printf '%s\n' "-P" "${port}"
  fi
}

remote() {
  local label="$1"
  local ip="$2"
  local port="$3"
  local host_key="$4"
  local command="$5"
  local args=()
  if [[ "${TRANSPORT}" == "ssh" ]]; then
    if [[ -n "${port}" ]]; then
      args=(-p "${port}")
    fi
    log_cmd "${label}" "ssh ${ip}${port:+:${port}} ${command}"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${args[@]}" "gatherlink@${ip}" "${command}"
  else
    if [[ -n "${port}" ]]; then
      args=(-P "${port}")
    fi
    log_cmd "${label}" "plink ${ip}${port:+:${port}} ${command}"
    "${PLINK}" -batch -agent -hostkey "${host_key}" "${args[@]}" -l gatherlink "${ip}" "${command}"
  fi
}

remote_capture() {
  local label="$1"
  local ip="$2"
  local port="$3"
  local host_key="$4"
  local command="$5"
  local output="$6"
  local args=()
  if [[ "${TRANSPORT}" == "ssh" ]]; then
    if [[ -n "${port}" ]]; then
      args=(-p "${port}")
    fi
    log_cmd "${label}" "ssh ${ip}${port:+:${port}} ${command} > ${output}"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${args[@]}" "gatherlink@${ip}" "${command}" >"${output}"
  else
    if [[ -n "${port}" ]]; then
      args=(-P "${port}")
    fi
    log_cmd "${label}" "plink ${ip}${port:+:${port}} ${command} > ${output}"
    "${PLINK}" -batch -agent -hostkey "${host_key}" "${args[@]}" -l gatherlink "${ip}" "${command}" >"${output}"
  fi
}

remote_a() {
  remote "$1" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "$2"
}

remote_b() {
  remote "$1" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "$2"
}

copy_file() {
  local label="$1"
  local ip="$2"
  local port="$3"
  local host_key="$4"
  local local_path="$5"
  local remote_path="$6"
  local args=()
  if [[ "${TRANSPORT}" == "ssh" ]]; then
    if [[ -n "${port}" ]]; then
      args=(-P "${port}")
    fi
    log_cmd "${label}" "scp ${local_path} ${ip}${port:+:${port}}:${remote_path}"
    scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${args[@]}" "${local_path}" "gatherlink@${ip}:${remote_path}"
  else
    if [[ -n "${port}" ]]; then
      args=(-P "${port}")
    fi
    local windows_path
    windows_path="$(wslpath -w "${local_path}")"
    log_cmd "${label}" "pscp ${windows_path} ${ip}${port:+:${port}}:${remote_path}"
    "${PSCP}" -batch -agent -hostkey "${host_key}" "${args[@]}" "${windows_path}" "gatherlink@${ip}:${remote_path}"
  fi
}

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

generate_rekey_material() {
  local work="${OUT_DIR}/provisioning"
  mkdir -p "${work}/node-a" "${work}/node-b"
  (
    cd "${REPO_ROOT}"
    .venv/bin/gatherlink secrets identity-create "${work}/issuer.identity.json" >/dev/null
    .venv/bin/gatherlink secrets identity-create "${work}/node-a.identity.json" >/dev/null
    .venv/bin/gatherlink secrets identity-create "${work}/node-b.identity.json" >/dev/null
    .venv/bin/gatherlink secrets identity-public "${work}/issuer.identity.json" >"${work}/issuer.public.json"
    .venv/bin/gatherlink secrets identity-public "${work}/node-a.identity.json" >"${work}/node-a.public.json"
    .venv/bin/gatherlink secrets identity-public "${work}/node-b.identity.json" >"${work}/node-b.public.json"
    .venv/bin/gatherlink secrets topology-create \
      --issuer "${work}/issuer.identity.json" \
      --output "${work}/topology.signed.json" \
      --generation 1 \
      --node "node-a=${work}/node-a.public.json" \
      --node "node-b=${work}/node-b.public.json" >/dev/null
    .venv/bin/gatherlink secrets noise-init \
      --local "${work}/node-a.identity.json" \
      --peer "${work}/node-b.public.json" \
      --topology "${work}/topology.signed.json" \
      --trust-root "${work}/issuer.public.json" \
      --initiation-output "${work}/noise-init.json" \
      --pending-output "${work}/noise-init.pending.secret.json" \
      --receiver-index 501 >/dev/null
    .venv/bin/gatherlink secrets noise-accept \
      --local "${work}/node-b.identity.json" \
      --topology "${work}/topology.signed.json" \
      --trust-root "${work}/issuer.public.json" \
      --initiation "${work}/noise-init.json" \
      --response-output "${work}/noise-response.json" \
      --security-output "${work}/node-b.security.secret.json" \
      --receiver-index 601 >/dev/null
    .venv/bin/gatherlink secrets noise-complete \
      --local "${work}/node-a.identity.json" \
      --topology "${work}/topology.signed.json" \
      --trust-root "${work}/issuer.public.json" \
      --pending "${work}/noise-init.pending.secret.json" \
      --response "${work}/noise-response.json" \
      --security-output "${work}/node-a.security.secret.json" >/dev/null
    .venv/bin/python - "${work}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

work = Path(sys.argv[1])
repo = Path.cwd()

def write_node_config(
    source_name: str,
    security_name: str,
    output: Path,
    *,
    rekey_after_packets: int,
    rekey_after_bytes: int,
) -> None:
    config = json.loads((repo / "configs" / "hyperv" / source_name).read_text(encoding="utf-8"))
    security = json.loads((work / security_name).read_text(encoding="utf-8"))
    security["rekey_after_packets"] = rekey_after_packets
    security["rekey_after_bytes"] = rekey_after_bytes
    config["security"] = security
    output.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

# Keep the live proof single-initiator: VM A crosses the low threshold while
# VM B accepts/responds but does not simultaneously originate a competing
# rotation. Collision handling can be tested later as a separate hardening
# scenario; this script is the release gate for the documented happy path.
write_node_config(
    "two-vm-node-a.json",
    "node-a.security.secret.json",
    work / "node-a" / "rekey-node-a.json",
    rekey_after_packets=20,
    rekey_after_bytes=4096,
)
write_node_config(
    "two-vm-node-b.json",
    "node-b.security.secret.json",
    work / "node-b" / "rekey-node-b.json",
    rekey_after_packets=10_000_000,
    rekey_after_bytes=10_000_000_000,
)
for name in ["node-a.identity.json", "node-b.public.json", "issuer.public.json", "topology.signed.json"]:
    (work / "node-a" / name).write_text((work / name).read_text(encoding="utf-8"), encoding="utf-8")
for name in ["node-b.identity.json", "node-a.public.json", "issuer.public.json", "topology.signed.json"]:
    (work / "node-b" / name).write_text((work / name).read_text(encoding="utf-8"), encoding="utf-8")
PY
  )
  tar -C "${work}/node-a" -czf "${OUT_DIR}/node-a-rekey-artifacts.tgz" .
  tar -C "${work}/node-b" -czf "${OUT_DIR}/node-b-rekey-artifacts.tgz" .
}

install_rekey_material() {
  copy_file "copy-rekey-a" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "${OUT_DIR}/node-a-rekey-artifacts.tgz" "/tmp/node-a-rekey-artifacts.tgz"
  copy_file "copy-rekey-b" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "${OUT_DIR}/node-b-rekey-artifacts.tgz" "/tmp/node-b-rekey-artifacts.tgz"
  remote_a "unpack-rekey-a" "rm -rf /tmp/gatherlink-rekey && mkdir -p /tmp/gatherlink-rekey && tar --warning=no-timestamp -xzf /tmp/node-a-rekey-artifacts.tgz -C /tmp/gatherlink-rekey && chmod 600 /tmp/gatherlink-rekey/node-a.identity.json /tmp/gatherlink-rekey/rekey-node-a.json"
  remote_b "unpack-rekey-b" "rm -rf /tmp/gatherlink-rekey && mkdir -p /tmp/gatherlink-rekey && tar --warning=no-timestamp -xzf /tmp/node-b-rekey-artifacts.tgz -C /tmp/gatherlink-rekey && chmod 600 /tmp/gatherlink-rekey/node-b.identity.json /tmp/gatherlink-rekey/rekey-node-b.json"
}

cleanup() {
  remote_a "cleanup-a" "cd /home/gatherlink/src/gatherlink && if [ -x .venv/bin/gatherlink ]; then .venv/bin/gatherlink services close rekey.vm.node-a >/dev/null 2>&1 || true; .venv/bin/gatherlink services prune >/dev/null 2>&1 || true; fi; for path in path-a path-b path-c; do sudo tc qdisc del dev \${path} root 2>/dev/null || true; sudo ip link set \${path} up; done"
  remote_b "cleanup-b" "cd /home/gatherlink/src/gatherlink && if [ -x .venv/bin/gatherlink ]; then .venv/bin/gatherlink services close rekey.vm.node-b >/dev/null 2>&1 || true; .venv/bin/gatherlink services prune >/dev/null 2>&1 || true; fi; for path in path-a path-b path-c; do sudo tc qdisc del dev \${path} root 2>/dev/null || true; sudo ip link set \${path} up; done"
}

start_services() {
  remote_b "start-rekey-b" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/rekey-node-b.jsonl && .venv/bin/gatherlink run start /tmp/gatherlink-rekey/rekey-node-b.json --name rekey.vm.node-b --diagnostics-jsonl /tmp/rekey-node-b.jsonl --rekey-local-identity /tmp/gatherlink-rekey/node-b.identity.json --rekey-peer-identity /tmp/gatherlink-rekey/node-a.public.json --rekey-topology /tmp/gatherlink-rekey/topology.signed.json --rekey-trust-root /tmp/gatherlink-rekey/issuer.public.json"
  sleep 1
  remote_a "start-rekey-a" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/rekey-node-a.jsonl && .venv/bin/gatherlink run start /tmp/gatherlink-rekey/rekey-node-a.json --name rekey.vm.node-a --diagnostics-jsonl /tmp/rekey-node-a.jsonl --rekey-local-identity /tmp/gatherlink-rekey/node-a.identity.json --rekey-peer-identity /tmp/gatherlink-rekey/node-b.public.json --rekey-topology /tmp/gatherlink-rekey/topology.signed.json --rekey-trust-root /tmp/gatherlink-rekey/issuer.public.json"
  sleep 2
}

send_trigger_traffic() {
  remote_b "trigger-receiver" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/rekey-trigger-received.txt; (timeout 30 .venv/bin/python tools/udp_probe.py receive 127.0.0.1:51820 --count ${COUNT} --min-count 1 --max-print-packets 0 > /tmp/rekey-trigger-received.txt 2>&1 & echo \$! > /tmp/rekey-trigger-receiver.pid)"
  sleep 1
  remote_a "trigger-send" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/udp_probe.py send 127.0.0.1:55180 rekey-trigger --count ${COUNT} --payload-size ${PAYLOAD_SIZE} > /tmp/rekey-trigger-sent.txt"
  sleep 4
}

wait_for_rekey_diagnostics() {
  for _ in $(seq 1 20); do
    if remote_a "check-rekey-a" "grep -q '\"code\":\"rekey.succeeded\"' /tmp/rekey-node-a.jsonl 2>/dev/null" \
      && remote_b "check-rekey-b" "grep -q '\"code\":\"rekey.succeeded\"' /tmp/rekey-node-b.jsonl 2>/dev/null"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

send_post_rekey_exact() {
  remote_b "post-rekey-receiver" "cd /home/gatherlink/src/gatherlink && rm -f /tmp/rekey-post-received.txt; (timeout 30 .venv/bin/python tools/udp_probe.py receive 127.0.0.1:51820 --count ${POST_REKEY_COUNT} > /tmp/rekey-post-received.txt 2>&1 & echo \$! > /tmp/rekey-post-receiver.pid)"
  sleep 1
  remote_a "post-rekey-send" "cd /home/gatherlink/src/gatherlink && .venv/bin/python tools/udp_probe.py send 127.0.0.1:55180 rekey-post --count ${POST_REKEY_COUNT}"
  sleep 2
  remote_b "post-rekey-verify" "test \$(grep -c '^rekey-post' /tmp/rekey-post-received.txt) -eq ${POST_REKEY_COUNT} && cat /tmp/rekey-post-received.txt"
}

capture_evidence() {
  remote_capture "diag-a" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "cat /tmp/rekey-node-a.jsonl 2>/dev/null || true" "${OUT_DIR}/diagnostics-node-a.jsonl"
  remote_capture "diag-b" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "cat /tmp/rekey-node-b.jsonl 2>/dev/null || true" "${OUT_DIR}/diagnostics-node-b.jsonl"
  remote_capture "status-a" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status rekey.vm.node-a" "${OUT_DIR}/status-node-a.json"
  remote_capture "status-b" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status rekey.vm.node-b" "${OUT_DIR}/status-node-b.json"
  remote_capture "monitor-a" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor rekey.vm.node-a --once" "${OUT_DIR}/monitor-node-a.txt"
  remote_capture "monitor-b" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services monitor rekey.vm.node-b --once" "${OUT_DIR}/monitor-node-b.txt"
  python3 "${REPO_ROOT}/tools/vm_acceptance/validate_jsonl.py" "${OUT_DIR}/diagnostics-node-a.jsonl" | tee "${OUT_DIR}/diagnostics-node-a.validation.txt"
  python3 "${REPO_ROOT}/tools/vm_acceptance/validate_jsonl.py" "${OUT_DIR}/diagnostics-node-b.jsonl" | tee "${OUT_DIR}/diagnostics-node-b.validation.txt"
  python3 - "${OUT_DIR}" "${REPORT}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
report = Path(sys.argv[2])
rows = []
required = {"rekey.started", "rekey.succeeded"}
for node in ["a", "b"]:
    path = out / f"diagnostics-node-{node}.jsonl"
    codes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            codes.append(json.loads(line).get("code"))
    missing = sorted(required - set(codes))
    if missing:
        raise SystemExit(f"{path} missing {', '.join(missing)}")
    rows.append((node, codes.count("rekey.started"), codes.count("rekey.succeeded")))
with report.open("a", encoding="utf-8") as handle:
    handle.write("\n### Rekey Diagnostics\n\n")
    handle.write("| node | rekey.started | rekey.succeeded |\n")
    handle.write("| --- | ---: | ---: |\n")
    for node, started, succeeded in rows:
        handle.write(f"| {node} | {started} | {succeeded} |\n")
PY
}

stop_services() {
  remote_a "stop-rekey-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close rekey.vm.node-a || true"
  remote_b "stop-rekey-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services close rekey.vm.node-b || true"
  remote_a "prune-rekey-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services prune || true"
  remote_b "prune-rekey-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services prune || true"
}

IP_A="$(hyperv_resolve_vm_ip "${REPO_ROOT}" "${SCRIPT_DIR}" "${VM_A}" "${IP_A}")"
IP_B="$(hyperv_resolve_vm_ip "${REPO_ROOT}" "${SCRIPT_DIR}" "${VM_B}" "${IP_B}")"

cat >"${REPORT}" <<REPORT
# Hyper-V Live Rekey Acceptance

- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- branch: ${BRANCH}
- vm_a: ${VM_A} ${IP_A}${PORT_A:+:${PORT_A}}
- vm_b: ${VM_B} ${IP_B}${PORT_B:+:${PORT_B}}
- trigger_packets: ${COUNT}
- post_rekey_packets: ${POST_REKEY_COUNT}
- payload_size: ${PAYLOAD_SIZE}

REPORT

step "Sync And Build"
sync_node "node-a" "${IP_A}" "${PORT_A}" "${HOST_KEY_A}"
sync_node "node-b" "${IP_B}" "${PORT_B}" "${HOST_KEY_B}"
record "source synced by Git and VM working trees reset to ${BRANCH}"

step "Provision"
generate_rekey_material
install_rekey_material
remote_a "validate-rekey-a" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/gatherlink-rekey/rekey-node-a.json"
remote_b "validate-rekey-b" "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink config validate /tmp/gatherlink-rekey/rekey-node-b.json"
record "temporary Noise-authenticated configs validate and use low rekey thresholds"

step "Start"
cleanup
start_services
record "both services started with explicit live-rekey identity, peer, topology, and trust-root inputs"

step "Trigger Rekey"
send_trigger_traffic
wait_for_rekey_diagnostics
record "autonomous rekey succeeded on both nodes while UDP traffic was moving"

step "Post-Rekey Delivery"
send_post_rekey_exact
record "post-rekey exact packet smoke delivered ${POST_REKEY_COUNT}/${POST_REKEY_COUNT} packets"

step "Evidence"
capture_evidence
record "diagnostics JSONL, status, and monitor snapshots captured"

step "Stop"
stop_services
record "managed services closed and pruned"

printf '\nReport written to %s\n' "${REPORT}"
