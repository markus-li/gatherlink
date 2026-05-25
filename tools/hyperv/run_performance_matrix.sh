#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
PORT_C="2203"
DURATION=20
PARALLEL=6
UDP_RATE="1000M"
UDP_LENGTH=1300
ACTIVE_PATHS="a,b,c"
SCENARIOS="private-lan,direct-wireguard,gatherlink-relay-udp,wireguard-over-gatherlink-relay"
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-matrix"
KEEP_RUNNING=0

usage() {
  cat <<'USAGE'
Usage: run_performance_matrix.sh [options]

Runs a repeatable performance comparison matrix. The point is separation of
concerns, not one heroic max-speed run:

  private-lan                         no Gatherlink, no WireGuard
  wireguard-kernel-onehop             kernel WireGuard, VM B -> VM A
  wireguard-userspace-onehop          wireguard-go, VM B -> VM A
  wireguard-gotatun-onehop            GotaTun, VM B -> VM A
  wireguard-boringtun-onehop          BoringTun, VM B -> VM A
  direct-wireguard                    WireGuard only, VM B -> C -> A route
  gatherlink-onehop-udp               Gatherlink only, VM B -> VM A
  gatherlink-relay-udp                Gatherlink only, VM B -> C -> A relay
  wireguard-over-gatherlink-relay     WireGuard over routed Gatherlink

Options:
  --ip IP              Management IP used with WSL portproxy. Default 172.22.0.1.
  --port-a PORT        SSH port for VM A. Default 2201.
  --port-b PORT        SSH port for VM B. Default 2202.
  --port-c PORT        SSH port for VM C. Default 2203.
  --duration SECONDS   Duration per scenario. Default 20.
  --parallel N         TCP parallel streams where applicable. Default 6.
  --udp-rate RATE      UDP offered rate where applicable. Default 1000M.
  --udp-length BYTES   UDP block size where applicable. Default 1300.
  --active-paths LIST   Comma-separated a,b,c paths where supported. Default a,b,c.
  --scenarios LIST     Comma-separated scenario names.
  --out DIR            Matrix output directory.
  --keep-running       Leave final scenario services up when supported.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --port-a) PORT_A="$2"; shift 2 ;;
    --port-b) PORT_B="$2"; shift 2 ;;
    --port-c) PORT_C="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --udp-rate) UDP_RATE="$2"; shift 2 ;;
    --udp-length) UDP_LENGTH="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --scenarios) SCENARIOS="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "${OUT_DIR}"
REPORT="${OUT_DIR}/report.md"
: >"${REPORT}"

record() {
  printf '%s\n' "$*" | tee -a "${REPORT}"
}

scenario_dir() {
  printf '%s/%02d-%s' "${OUT_DIR}" "$1" "$2"
}

record "# Hyper-V Performance Matrix"
record ""
record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
record "- duration_seconds: ${DURATION}"
record "- tcp_parallel: ${PARALLEL}"
record "- udp_rate: ${UDP_RATE}"
record "- udp_length: ${UDP_LENGTH}"
record "- active_paths: ${ACTIVE_PATHS}"
record "- scenarios: ${SCENARIOS}"
record ""

IFS=',' read -r -a scenario_array <<<"${SCENARIOS}"
index=1
for scenario in "${scenario_array[@]}"; do
  scenario="$(printf '%s' "${scenario}" | xargs)"
  out="$(scenario_dir "${index}" "${scenario}")"
  record "## ${scenario}"
  case "${scenario}" in
    private-lan)
      "${SCRIPT_DIR}/run_private_lan_speed.sh" \
        --ip "${IP}" --port-a "${PORT_A}" --port-b "${PORT_B}" \
        --duration "${DURATION}" --parallel "${PARALLEL}" \
        --udp-rate "${UDP_RATE}" --udp-length "${UDP_LENGTH}" --out "${out}"
      ;;
    wireguard-kernel-onehop|wireguard-userspace-onehop|wireguard-gotatun-onehop|wireguard-boringtun-onehop)
      implementation="kernel"
      if [[ "${scenario}" == "wireguard-userspace-onehop" ]]; then
        implementation="userspace"
      elif [[ "${scenario}" == "wireguard-gotatun-onehop" ]]; then
        implementation="gotatun"
      elif [[ "${scenario}" == "wireguard-boringtun-onehop" ]]; then
        implementation="boringtun"
      fi
      "${SCRIPT_DIR}/run_wireguard_onehop_speed.sh" \
        --implementation "${implementation}" \
        --ip "${IP}" --port-a "${PORT_A}" --port-b "${PORT_B}" \
        --duration "${DURATION}" --parallel "${PARALLEL}" \
        --udp-rate "${UDP_RATE}" --udp-length "${UDP_LENGTH}" \
        --active-paths "${ACTIVE_PATHS}" --out "${out}"
      ;;
    direct-wireguard)
      "${SCRIPT_DIR}/run_direct_wireguard_routing_speed.sh" \
        --ip "${IP}" --port-a "${PORT_A}" --port-b "${PORT_B}" --port-c "${PORT_C}" \
        --duration "${DURATION}" --parallel "${PARALLEL}" --out "${out}"
      ;;
    gatherlink-onehop-udp)
      target_mbit="${UDP_RATE}"
      target_mbit="${target_mbit%M}"
      target_mbit="${target_mbit%m}"
      "${SCRIPT_DIR}/run_gatherlink_onehop_speed.sh" \
        --ip "${IP}" --port-a "${PORT_A}" --port-b "${PORT_B}" \
        --duration "${DURATION}" --payload-size "${UDP_LENGTH}" --target-mbit "${target_mbit}" \
        --path-mtu 1472 --active-paths "${ACTIVE_PATHS}" --out "${out}"
      ;;
    gatherlink-relay-udp)
      target_mbit="${UDP_RATE}"
      target_mbit="${target_mbit%M}"
      target_mbit="${target_mbit%m}"
      "${SCRIPT_DIR}/run_relay_udp_speed.sh" \
        --ip "${IP}" --port-a "${PORT_A}" --port-b "${PORT_B}" --port-c "${PORT_C}" \
        --duration "${DURATION}" --payload-size "${UDP_LENGTH}" --target-mbit "${target_mbit}" \
        --path-mtu 1472 --active-paths "${ACTIVE_PATHS}" --out "${out}"
      ;;
    wireguard-over-gatherlink-relay)
      keep_arg=()
      if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
        keep_arg=(--keep-running)
      fi
      "${SCRIPT_DIR}/run_relay_wireguard_speed.sh" \
        --ip "${IP}" --port-a "${PORT_A}" --port-b "${PORT_B}" --port-c "${PORT_C}" \
        --duration "${DURATION}" --parallel "${PARALLEL}" \
        --udp-rate "${UDP_RATE}" --udp-length "${UDP_LENGTH}" \
        --active-paths "${ACTIVE_PATHS}" --out "${out}" \
        "${keep_arg[@]}"
      ;;
    *)
      echo "unknown scenario: ${scenario}" >&2
      exit 2
      ;;
  esac
  if [[ -f "${out}/report.json" ]]; then
    python3 - <<'PY' "${out}/report.json" | tee -a "${REPORT}"
from __future__ import annotations

import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for result in data.get("results", []):
    extras = []
    if "lost_percent" in result:
        extras.append(f"lost={result['lost_percent']:.2f}%")
    if "retransmits" in result:
        extras.append(f"retrans={result['retransmits']}")
    suffix = f" {' '.join(extras)}" if extras else ""
    print(f"- {result['name']}: {result['mbit_per_second']:.2f} Mbit/s{suffix}")
PY
  else
    record "- report: ${out}/report.md"
  fi
  record ""
  index=$((index + 1))
done

python3 - "${OUT_DIR}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
matrix = {"output": str(out), "scenarios": []}
for report in sorted(out.glob("*/report.json")):
    data = json.loads(report.read_text(encoding="utf-8"))
    matrix["scenarios"].append({"name": report.parent.name, "report": str(report), "results": data.get("results", [])})
(out / "matrix.json").write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

record "Matrix JSON: ${OUT_DIR}/matrix.json"
printf '\nPerformance matrix complete.\nReport: %s\n' "${REPORT}"
