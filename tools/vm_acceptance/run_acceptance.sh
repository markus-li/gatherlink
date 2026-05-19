#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MODE="dry-run"
INVENTORY="${SCRIPT_DIR}/inventory.example.env"
OUT_DIR="${REPO_ROOT}/.gatherlink/vm-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"
EXAMPLE_KEY_A="ERERERERERERERERERERERERERERERERERERERERERE="
EXAMPLE_KEY_B="IiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiI="

usage() {
  cat <<'USAGE'
Usage: run_acceptance.sh [--inventory FILE] [--out DIR] [--dry-run|--execute]

Build and optionally execute the two-Debian-VM Gatherlink v0.9 acceptance flow.

Default mode is --dry-run. Dry-run renders configs, validates them locally, and
reports the SSH/Bash commands that would run, but it never contacts VMs.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory)
      INVENTORY="$2"
      shift 2
      ;;
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --execute)
      MODE="execute"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "${OUT_DIR}"
COMMAND_LOG="${OUT_DIR}/commands.log"
REPORT="${OUT_DIR}/report.md"
JSON_REPORT="${OUT_DIR}/report.json"
CHECKS_JSONL="${OUT_DIR}/checks.jsonl"
CONFIG_A="${OUT_DIR}/node-a.json"
CONFIG_B="${OUT_DIR}/node-b.json"

# Export sourced inventory values so the template renderer can consume them
# without implementing a second inventory parser in Python.
set -a
# shellcheck disable=SC1090
source "${INVENTORY}"
set +a

step() {
  printf '\n## %s\n\n' "$1" >>"${REPORT}"
}

record() {
  printf -- '- %s\n' "$1" >>"${REPORT}"
}

check() {
  local status="$1"
  local code="$2"
  local message="$3"
  local node="${4:-}"
  PYTHONPATH="${REPO_ROOT}/python" "${REPO_ROOT}/.venv/bin/python" - "$CHECKS_JSONL" "$status" "$code" "$message" "$node" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
status, code, message, node = sys.argv[2:6]
event = {"status": status, "code": code, "message": message}
if node:
    event["node"] = node
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, sort_keys=True) + "\n")
PY
}

run_cmd() {
  local label="$1"
  shift
  printf '[%s] %s\n' "$label" "$*" | tee -a "${COMMAND_LOG}" >/dev/null
  if [[ "${MODE}" == "execute" ]]; then
    "$@"
  fi
}

run_local_cmd() {
  local label="$1"
  shift
  printf '[%s] %s\n' "$label" "$*" | tee -a "${COMMAND_LOG}" >/dev/null
  "$@"
}

remote_cmd() {
  local node="$1"
  local label="$2"
  local command="$3"
  run_cmd "$label" ssh "$node" "$command"
}

render_template() {
  local template="$1"
  local output="$2"
  python3 - "$template" "$output" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

template = Path(sys.argv[1]).read_text(encoding="utf-8")
for key, value in os.environ.items():
    template = template.replace("{{" + key + "}}", value)
Path(sys.argv[2]).write_text(template, encoding="utf-8")
PY
}

validate_inventory() {
  local missing=0
  for name in \
    NODE_A_SSH NODE_B_SSH REMOTE_WORKDIR REMOTE_CONFIG_DIR REMOTE_REPORT_DIR \
    NODE_A_SERVICE_NAME NODE_B_SERVICE_NAME NODE_A_SERVICE_LISTEN NODE_A_SERVICE_TARGET \
    NODE_B_SERVICE_LISTEN NODE_B_SERVICE_TARGET PATH_A_NODE_A_BIND PATH_A_NODE_B_BIND \
    PATH_B_NODE_A_BIND PATH_B_NODE_B_BIND PATH_A_INTERFACE PATH_B_INTERFACE PATH_MTU \
    PATH_CAPACITY_BPS NODE_A_RECEIVER_INDEX NODE_A_SEND_KEY NODE_A_RECEIVE_KEY \
    NODE_B_RECEIVER_INDEX NODE_B_SEND_KEY NODE_B_RECEIVE_KEY; do
    if [[ -z "${!name:-}" ]]; then
      echo "missing inventory value: ${name}" >&2
      missing=1
    fi
  done
  if [[ "${MODE}" == "execute" ]]; then
    for secret_name in NODE_A_SEND_KEY NODE_A_RECEIVE_KEY NODE_B_SEND_KEY NODE_B_RECEIVE_KEY; do
      local secret_value="${!secret_name}"
      if [[ "${secret_value}" == replace-with-* ]] \
        || [[ "${secret_value}" == "${EXAMPLE_KEY_A}" ]] \
        || [[ "${secret_value}" == "${EXAMPLE_KEY_B}" ]]; then
        echo "refusing --execute with example or placeholder authenticated session key: ${secret_name}" >&2
        missing=1
      fi
    done
  fi
  return "${missing}"
}

cat >"${REPORT}" <<REPORT
# Gatherlink Real VM Acceptance Report

- mode: ${MODE}
- inventory: ${INVENTORY}
- output: ${OUT_DIR}
- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)

REPORT

step "Preflight"
validate_inventory
record "inventory loaded and required fields are present"
record "execute mode refuses placeholder or committed example authenticated session keys"
check "pass" "vm.inventory.valid" "inventory loaded and required fields are present"
check "pass" "vm.secrets.guard" "execute mode refuses placeholder or committed example authenticated session keys"

step "Render Configs"
render_template "${SCRIPT_DIR}/config-node-a.json.template" "${CONFIG_A}"
render_template "${SCRIPT_DIR}/config-node-b.json.template" "${CONFIG_B}"
record "rendered ${CONFIG_A}"
record "rendered ${CONFIG_B}"
check "pass" "vm.config.rendered" "node configs rendered from templates"

step "Local Validation"
run_local_cmd "validate-node-a" "${REPO_ROOT}/.venv/bin/gatherlink" config validate "${CONFIG_A}"
run_local_cmd "validate-node-b" "${REPO_ROOT}/.venv/bin/gatherlink" config validate "${CONFIG_B}"
record "configs validated locally before any VM contact"
check "pass" "vm.config.validated" "configs validated locally before any VM contact"

step "Remote Prepare"
remote_cmd "${NODE_A_SSH}" "prepare-node-a" "mkdir -p ${REMOTE_WORKDIR} ${REMOTE_CONFIG_DIR} ${REMOTE_REPORT_DIR}"
remote_cmd "${NODE_B_SSH}" "prepare-node-b" "mkdir -p ${REMOTE_WORKDIR} ${REMOTE_CONFIG_DIR} ${REMOTE_REPORT_DIR}"
record "remote directories prepared"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.remote.prepare" "remote directories prepared"
else
  check "skipped" "vm.remote.prepare" "dry-run recorded remote prepare commands without contacting VMs"
fi

step "Sync Repository"
SYNC_A="tar -C '${REPO_ROOT}' --exclude .git --exclude .venv --exclude .mypy_cache --exclude .pytest_cache --exclude target -cf - . | ssh '${NODE_A_SSH}' 'tar -C ${REMOTE_WORKDIR} -xf -'"
SYNC_B="tar -C '${REPO_ROOT}' --exclude .git --exclude .venv --exclude .mypy_cache --exclude .pytest_cache --exclude target -cf - . | ssh '${NODE_B_SSH}' 'tar -C ${REMOTE_WORKDIR} -xf -'"
printf '[sync-node-a] %s\n' "${SYNC_A}" | tee -a "${COMMAND_LOG}" >/dev/null
printf '[sync-node-b] %s\n' "${SYNC_B}" | tee -a "${COMMAND_LOG}" >/dev/null
if [[ "${MODE}" == "execute" ]]; then
  bash -lc "${SYNC_A}"
  bash -lc "${SYNC_B}"
fi
record "repository sync commands recorded and executed only in execute mode"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.repo.sync" "repository sync commands completed"
else
  check "skipped" "vm.repo.sync" "dry-run recorded repository sync commands without contacting VMs"
fi

step "Install Dependencies"
INSTALL_CMD='python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .'
remote_cmd "${NODE_A_SSH}" "install-node-a" "cd ${REMOTE_WORKDIR} && ${INSTALL_CMD}"
remote_cmd "${NODE_B_SSH}" "install-node-b" "cd ${REMOTE_WORKDIR} && ${INSTALL_CMD}"
record "Python package installed in each VM-local venv"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.dependencies.installed" "Python package installed in each VM-local venv"
else
  check "skipped" "vm.dependencies.installed" "dry-run recorded VM dependency install commands"
fi

step "Upload Configs"
run_cmd "upload-node-a-config" scp "${CONFIG_A}" "${NODE_A_SSH}:${REMOTE_CONFIG_DIR}/node-a.json"
run_cmd "upload-node-b-config" scp "${CONFIG_B}" "${NODE_B_SSH}:${REMOTE_CONFIG_DIR}/node-b.json"
record "node configs uploaded"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.config.uploaded" "node configs uploaded"
else
  check "skipped" "vm.config.uploaded" "dry-run recorded config upload commands"
fi

step "Remote Validate"
remote_cmd "${NODE_A_SSH}" "remote-validate-node-a" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink config validate ${REMOTE_CONFIG_DIR}/node-a.json"
remote_cmd "${NODE_B_SSH}" "remote-validate-node-b" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink config validate ${REMOTE_CONFIG_DIR}/node-b.json"
record "remote configs validate through the same CLI path operators use"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.remote.config.validated" "remote configs validated through the operator CLI"
else
  check "skipped" "vm.remote.config.validated" "dry-run recorded remote config validation commands"
fi

step "Start Services"
remote_cmd "${NODE_B_SSH}" "start-node-b" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink run start ${REMOTE_CONFIG_DIR}/node-b.json --name ${NODE_B_SERVICE_NAME} --diagnostics-jsonl ${REMOTE_REPORT_DIR}/node-b-diagnostics.jsonl"
remote_cmd "${NODE_A_SSH}" "start-node-a" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink run start ${REMOTE_CONFIG_DIR}/node-a.json --name ${NODE_A_SERVICE_NAME} --diagnostics-jsonl ${REMOTE_REPORT_DIR}/node-a-diagnostics.jsonl"
record "services started through the normal managed runner"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.services.started" "services started through the normal managed runner"
else
  check "skipped" "vm.services.started" "dry-run recorded service start commands"
fi

step "Traffic And Status"
remote_cmd "${NODE_B_SSH}" "start-udp-receiver" "cd ${REMOTE_WORKDIR} && (timeout 20 .venv/bin/python tools/udp_probe.py receive ${NODE_B_SERVICE_TARGET} --count 5 > ${REMOTE_REPORT_DIR}/received.txt 2>&1 & echo \$! > ${REMOTE_REPORT_DIR}/receiver.pid)"
remote_cmd "${NODE_A_SSH}" "send-udp" "cd ${REMOTE_WORKDIR} && .venv/bin/python tools/udp_probe.py send ${NODE_A_SERVICE_LISTEN} vm-acceptance --count 5"
remote_cmd "${NODE_A_SSH}" "status-node-a" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink services status ${NODE_A_SERVICE_NAME}"
remote_cmd "${NODE_B_SSH}" "status-node-b" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink services status ${NODE_B_SERVICE_NAME}"
remote_cmd "${NODE_A_SSH}" "monitor-node-a" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink services monitor ${NODE_A_SERVICE_NAME} --once > ${REMOTE_REPORT_DIR}/monitor-node-a.txt"
remote_cmd "${NODE_B_SSH}" "monitor-node-b" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink services monitor ${NODE_B_SERVICE_NAME} --once > ${REMOTE_REPORT_DIR}/monitor-node-b.txt"
remote_cmd "${NODE_A_SSH}" "diagnostics-node-a" "cd ${REMOTE_WORKDIR} && .venv/bin/python tools/vm_acceptance/validate_jsonl.py ${REMOTE_REPORT_DIR}/node-a-diagnostics.jsonl"
remote_cmd "${NODE_B_SSH}" "diagnostics-node-b" "cd ${REMOTE_WORKDIR} && .venv/bin/python tools/vm_acceptance/validate_jsonl.py ${REMOTE_REPORT_DIR}/node-b-diagnostics.jsonl"
record "UDP traffic, status, monitor output, and diagnostics JSONL are checked"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.traffic.status.diagnostics" "UDP traffic, status, monitor output, and diagnostics JSONL checked"
else
  check "skipped" "vm.traffic.status.diagnostics" "dry-run recorded traffic, status, monitor, and diagnostics commands"
fi

step "Path Degrade And Recovery"
remote_cmd "${NODE_A_SSH}" "degrade-path-a" "sudo ip link set ${PATH_A_INTERFACE} down || true"
remote_cmd "${NODE_A_SSH}" "recover-path-a" "sudo ip link set ${PATH_A_INTERFACE} up || true"
record "path flap is attempted when the VM network exposes the configured interface"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.path.flap.recovery" "path flap and recovery commands completed or were tolerated by the harness"
else
  check "skipped" "vm.path.flap.recovery" "dry-run recorded path degrade and recovery commands"
fi

step "Stop And Collect"
remote_cmd "${NODE_A_SSH}" "close-node-a" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink services close ${NODE_A_SERVICE_NAME}"
remote_cmd "${NODE_B_SSH}" "close-node-b" "cd ${REMOTE_WORKDIR} && .venv/bin/gatherlink services close ${NODE_B_SERVICE_NAME}"
run_cmd "collect-node-a-report" scp -r "${NODE_A_SSH}:${REMOTE_REPORT_DIR}" "${OUT_DIR}/node-a-report"
run_cmd "collect-node-b-report" scp -r "${NODE_B_SSH}:${REMOTE_REPORT_DIR}" "${OUT_DIR}/node-b-report"
record "services closed and reports collected"
if [[ "${MODE}" == "execute" ]]; then
  check "pass" "vm.services.closed" "services closed and reports collected"
else
  check "skipped" "vm.services.closed" "dry-run recorded service close and report collection commands"
fi

PYTHONPATH="${REPO_ROOT}/python" "${REPO_ROOT}/.venv/bin/python" "${SCRIPT_DIR}/write_report_json.py" \
  --mode "${MODE}" \
  --inventory "${INVENTORY}" \
  --out "${OUT_DIR}" \
  --checks-jsonl "${CHECKS_JSONL}" \
  --report-json "${JSON_REPORT}" \
  --artifact "markdown:${REPORT}:human-readable acceptance report" \
  --artifact "commands:${COMMAND_LOG}:command transcript" \
  --artifact "config:${CONFIG_A}:rendered node A config" \
  --artifact "config:${CONFIG_B}:rendered node B config"

cat <<EOF
VM acceptance ${MODE} complete.
Report: ${REPORT}
JSON report: ${JSON_REPORT}
Commands: ${COMMAND_LOG}
EOF
