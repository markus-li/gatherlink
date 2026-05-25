#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/perf_common.sh"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
DURATION=20
PARALLEL=6
UDP_RATE="1000M"
UDP_LENGTH=1200
ACTIVE_PATHS="a,b,c"
LINK_MTU=1500
UDP_PRESSURE_UNBOUNDED=0
OUT_DIR="$(perf_repo_root)/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-private-lan"

usage() {
  cat <<'USAGE'
Usage: run_private_lan_speed.sh [options]

Measures the raw VM private LAN links without WireGuard and without
Gatherlink. This is the first baseline for any performance investigation.

Options:
  --ip IP              Management IP used with the WSL portproxy setup. Default 172.22.0.1.
  --port-a PORT        SSH port for VM A. Default 2201.
  --port-b PORT        SSH port for VM B. Default 2202.
  --duration SECONDS   iperf duration per run. Default 20.
  --parallel N         TCP parallel streams. Default 6.
  --udp-rate RATE      UDP offered rate per path. Default 1000M.
  --udp-length BYTES   UDP block size. Default 1200.
  --active-paths LIST  Comma-separated path letters. Default a,b,c; supports a,b,c,d,e.
  --link-mtu BYTES     Linux path interface MTU on VM A and VM B. Default 1500.
  --udp-pressure-unbounded
                        Run the udp_pressure generator without pacing in the simultaneous section.
  --out DIR            Report directory.
  --skip-kernel-tuning Do not apply UDP socket-buffer sysctls first.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --port-a) PORT_A="$2"; shift 2 ;;
    --port-b) PORT_B="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --udp-rate) UDP_RATE="$2"; shift 2 ;;
    --udp-length) UDP_LENGTH="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --link-mtu) LINK_MTU="$2"; shift 2 ;;
    --udp-pressure-unbounded) UDP_PRESSURE_UNBOUNDED=1; shift ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --skip-kernel-tuning) PERF_APPLY_KERNEL_TUNING=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

perf_init_defaults
REPORT="${OUT_DIR}/report.md"
: >"${REPORT}"
trap perf_cleanup_all EXIT

path_indexes() {
  perf_path_indexes "${ACTIVE_PATHS}"
}

udp_rate_mbit() {
  python3 - "$UDP_RATE" <<'PY'
import sys

value = sys.argv[1].strip()
suffix = value[-1:].lower()
number = value[:-1] if suffix.isalpha() else value
rate = float(number)
if suffix == "k":
    rate /= 1000
elif suffix == "g":
    rate *= 1000
elif suffix.isalpha() and suffix != "m":
    raise SystemExit(f"unsupported UDP rate suffix: {value}")
print(rate)
PY
}

perf_record "# Private LAN Speed Baseline"
perf_record ""
perf_record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
perf_record "- duration_seconds: ${DURATION}"
perf_record "- tcp_parallel: ${PARALLEL}"
perf_record "- udp_rate: ${UDP_RATE}"
perf_record "- udp_length: ${UDP_LENGTH}"
perf_record "- active_paths: ${ACTIVE_PATHS}"
perf_record "- link_mtu: ${LINK_MTU}"
perf_record "- udp_pressure_unbounded: ${UDP_PRESSURE_UNBOUNDED}"
perf_record "- output: ${OUT_DIR}"
perf_record ""

perf_cleanup_all
if [[ "${PERF_APPLY_KERNEL_TUNING}" -eq 1 ]]; then
  perf_step "Kernel Tuning"
  perf_apply_kernel_tuning
  perf_record "Applied lab UDP socket-buffer tuning to all configured nodes."
fi
for index in $(path_indexes); do
  letter="$(printf '%b' "\\$(printf '%03o' "$((96 + index))")")"
  perf_remote_a "sudo ip link set path-${letter} mtu ${LINK_MTU} up"
  perf_remote_b "sudo ip link set path-${letter} mtu ${LINK_MTU} up"
done
perf_compile_udp_pressure "${PORT_A}"
perf_compile_udp_pressure "${PORT_B}"

perf_step "Node Snapshots"
perf_collect_node_snapshot "vm-a" "${PORT_A}"
perf_collect_node_snapshot "vm-b" "${PORT_B}"
perf_record "Captured host/kernel/interface snapshots."

perf_step "Per-Path TCP"
for index in $(path_indexes); do
  perf_run_iperf_tcp "path-${index}-tcp" "${PORT_A}" "${PORT_B}" "10.91.${index}.11" "10.91.${index}.11" "$((7600 + index))" "${PARALLEL}" "${DURATION}"
done

perf_step "Per-Path UDP"
for index in $(path_indexes); do
  perf_run_iperf_udp "path-${index}-udp" "${PORT_A}" "${PORT_B}" "10.91.${index}.11" "10.91.${index}.11" "$((7610 + index))" "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
done

perf_step "Simultaneous UDP"
for index in $(path_indexes); do
  label="path-${index}-udp-simultaneous"
  perf_start_iperf_udp_server "${label}" "${PORT_A}" "10.91.${index}.11" "$((7710 + index))"
done
sleep 1
for index in $(path_indexes); do
  label="path-${index}-udp-simultaneous"
  perf_start_iperf_udp_client_background "${label}" "${PORT_B}" "10.91.${index}.11" "$((7710 + index))" "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
done
sleep "$((DURATION + 3))"
for index in $(path_indexes); do
  label="path-${index}-udp-simultaneous"
  perf_fetch_iperf_udp_background "${label}" "${PORT_A}" "${PORT_B}"
done

perf_step "UDP Pressure Simultaneous"
pressure_mbit="$(udp_rate_mbit)"
if [[ "${UDP_PRESSURE_UNBOUNDED}" -eq 1 ]]; then
  pressure_mbit=""
fi
for index in $(path_indexes); do
  label="path-${index}-udp-pressure-simultaneous"
  perf_start_udp_pressure_sink "${label}" "${PORT_A}" "10.91.${index}.11:$((7810 + index))" "${DURATION}"
done
sleep 1
for index in $(path_indexes); do
  label="path-${index}-udp-pressure-simultaneous"
  perf_start_udp_pressure_client_background "${label}" "${PORT_B}" "10.91.${index}.11:$((7810 + index))" "${DURATION}" "${UDP_LENGTH}" "${pressure_mbit}"
done
sleep "$((DURATION + 3))"
for index in $(path_indexes); do
  label="path-${index}-udp-pressure-simultaneous"
  perf_fetch_udp_pressure_background "${label}" "${PORT_A}" "${PORT_B}"
done

perf_step "Summary"
perf_summarize_iperf_jsons | tee -a "${REPORT}"
perf_record ""
perf_record "JSON summary: ${REPORT_JSON}"
printf '\nPrivate LAN speed baseline complete.\nReport: %s\n' "${REPORT}"
