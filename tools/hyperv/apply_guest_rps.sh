#!/usr/bin/env bash
set -euo pipefail

IP="172.22.0.1"
PORTS="2201,2202,2203"
PERF_USER="${PERF_USER:-gatherlink}"
ACTIVE_PATHS="a,b,c"
RPS_CPUS="f"
RPS_FLOW_CNT=4096
RPS_SOCK_FLOW_ENTRIES=32768

usage() {
  cat <<'USAGE'
Usage: apply_guest_rps.sh [options]

Applies receive packet steering settings to Gatherlink Hyper-V Debian lab path
interfaces. This is benchmark/lab tuning only; it is not a Gatherlink runtime
requirement.

Options:
  --ip IP                 Management IP used with WSL portproxy. Default 172.22.0.1.
  --ports LIST            Comma-separated SSH ports. Default 2201,2202,2203.
  --active-paths LIST     Comma-separated path letters. Default a,b,c.
  --rps-cpus MASK         CPU mask written to rx-*/rps_cpus. Default f.
  --rps-flow-cnt N        Per-RX-queue rps_flow_cnt. Default 4096.
  --sock-flow-entries N   net.core.rps_sock_flow_entries. Default 32768.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --ports) PORTS="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --rps-cpus) RPS_CPUS="$2"; shift 2 ;;
    --rps-flow-cnt) RPS_FLOW_CNT="$2"; shift 2 ;;
    --sock-flow-entries) RPS_SOCK_FLOW_ENTRIES="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

remote_apply() {
  local port="$1"
  local paths="$2"
  local command
  command="$(printf 'sudo sh -s -- %q %q %q %q' "${paths}" "${RPS_CPUS}" "${RPS_FLOW_CNT}" "${RPS_SOCK_FLOW_ENTRIES}")"
  ssh -n -p "${port}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${PERF_USER}@${IP}" "${command}" <<'REMOTE'
set -eu
paths="$1"
rps_cpus="$2"
rps_flow_cnt="$3"
sock_flow_entries="$4"

echo "${sock_flow_entries}" > /proc/sys/net/core/rps_sock_flow_entries
old_ifs="${IFS}"
IFS=","
for path in ${paths}; do
  path="$(printf '%s' "${path}" | tr -d '[:space:]')"
  [ -n "${path}" ] || continue
  for queue in /sys/class/net/path-${path}/queues/rx-*; do
    [ -d "${queue}" ] || continue
    [ -w "${queue}/rps_cpus" ] && echo "${rps_cpus}" > "${queue}/rps_cpus"
    [ -w "${queue}/rps_flow_cnt" ] && echo "${rps_flow_cnt}" > "${queue}/rps_flow_cnt"
  done
done
IFS="${old_ifs}"
REMOTE
}

IFS=',' read -r -a ports <<<"${PORTS}"
for port in "${ports[@]}"; do
  [[ -n "${port}" ]] || continue
  remote_apply "${port}" "${ACTIVE_PATHS}"
done
