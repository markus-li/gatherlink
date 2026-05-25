#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
DURATION=8
PAYLOAD_SIZE=1300
RAW_TARGET_MBIT=2500
PARALLEL=24
UDP_RATE="2000M"
UDP_LENGTH=1300
LINK_MTU=1500
WG_MTU=1380
PATH_MTU=1472
CORE_BATCH_SIZE=512
SECURITY_MODE="authenticated"
ACTIVE_PATHS="a,b,c"
PATH_CAPACITY_MBITS="a:5000,b:5000,c:5000"
FLOWLET_IDLE_US=50000
FLOWLET_MAX_HOLD_US=60000000
PATH_RUN_DATAGRAMS=0
REORDER_HOLD_US=2000
SHAPE_PROFILE="clean"
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-wireguard-gatherlink-scheduler-sweep"
SCHEDULERS="round_robin,capacity_aware,arrival_guarded_capacity,latency_guarded_capacity,flowlet_adaptive,coordinated_adaptive,ordered_multipath_capacity_aware"

usage() {
  cat <<'USAGE'
Usage: run_wireguard_gatherlink_scheduler_sweep.sh [options]

Runs paired raw-Gatherlink and WireGuard-over-Gatherlink one-hop benchmarks for
each scheduler. This is meant to answer whether WireGuard's one encrypted UDP
flow wants a different Gatherlink scheduler than raw UDP payload traffic.

Options:
  --ip IP                  Management IP used with WSL portproxy. Default 172.22.0.1.
  --port-a PORT            SSH port for VM A. Default 2201.
  --port-b PORT            SSH port for VM B. Default 2202.
  --duration SECONDS       Duration for each raw/WireGuard run. Default 8.
  --schedulers LIST        Comma-separated scheduler list.
  --raw-target-mbit MBIT   Raw Gatherlink offered rate. Default 2500.
  --payload-size BYTES     Raw Gatherlink UDP payload size. Default 1300.
  --parallel N             WireGuard TCP parallel streams. Default 24.
  --udp-rate RATE          WireGuard UDP offered rate. Default 2000M.
  --udp-length BYTES       WireGuard UDP block size. Default 1300.
  --link-mtu BYTES         Linux path interface MTU. Default 1500.
  --wg-mtu BYTES           WireGuard interface MTU. Default 1380.
  --path-mtu BYTES         Gatherlink path MTU. Default 1472.
  --core-batch-size N      Gatherlink core batch size. Default 512.
  --security-mode MODE     Gatherlink security mode. Default authenticated.
  --active-paths LIST      Comma-separated a,b,c path list. Default a,b,c.
  --path-capacity-mbits SPEC
                           Scheduler capacity hints. Default a:5000,b:5000,c:5000.
  --flowlet-idle-us N      Gatherlink flowlet idle timeout. Default 50000.
  --flowlet-max-hold-us N  Gatherlink max continuous hold. Default 60000000.
  --path-run-datagrams N   Maximum hot-burst datagrams per path before rescheduling. Default 0.
  --reorder-hold-us N      Gatherlink reorder hold. Default 2000.
  --shape-profile NAME     Hyper-V path shaping profile. Default clean.
  --out DIR                Report directory.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --port-a) PORT_A="$2"; shift 2 ;;
    --port-b) PORT_B="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --schedulers) SCHEDULERS="$2"; shift 2 ;;
    --raw-target-mbit) RAW_TARGET_MBIT="$2"; shift 2 ;;
    --payload-size) PAYLOAD_SIZE="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --udp-rate) UDP_RATE="$2"; shift 2 ;;
    --udp-length) UDP_LENGTH="$2"; shift 2 ;;
    --link-mtu) LINK_MTU="$2"; shift 2 ;;
    --wg-mtu) WG_MTU="$2"; shift 2 ;;
    --path-mtu) PATH_MTU="$2"; shift 2 ;;
    --core-batch-size) CORE_BATCH_SIZE="$2"; shift 2 ;;
    --security-mode) SECURITY_MODE="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --path-capacity-mbits) PATH_CAPACITY_MBITS="$2"; shift 2 ;;
    --flowlet-idle-us) FLOWLET_IDLE_US="$2"; shift 2 ;;
    --flowlet-max-hold-us) FLOWLET_MAX_HOLD_US="$2"; shift 2 ;;
    --path-run-datagrams) PATH_RUN_DATAGRAMS="$2"; shift 2 ;;
    --reorder-hold-us) REORDER_HOLD_US="$2"; shift 2 ;;
    --shape-profile) SHAPE_PROFILE="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "${OUT_DIR}"
REPORT="${OUT_DIR}/report.md"
SUMMARY_TSV="${OUT_DIR}/summary.tsv"
: >"${REPORT}"

record() {
  printf '%s\n' "$*" | tee -a "${REPORT}"
}

extract_raw_sink_mbit() {
  local report="$1"
  awk '/^sink:/ {for (i = 1; i <= NF; i++) if ($i ~ /^Mbit\/s$/) print $(i - 1)}' "${report}" | tail -1
}

extract_raw_delta() {
  local report="$1"
  awk -F': ' '/^application_packet_delta:/ {print $2}' "${report}" | tail -1
}

extract_wg_tcp_mbit() {
  local report="$1"
  awk -F'[: ]+' '/wg-over-gl-onehop-tcp/ {print $3}' "${report}" | tail -1
}

extract_wg_udp_mbit() {
  local report="$1"
  awk -F'[: ]+' '/wg-over-gl-onehop-udp/ {print $3}' "${report}" | tail -1
}

extract_wg_udp_loss() {
  local report="$1"
  awk -F'lost=' '/wg-over-gl-onehop-udp/ {gsub("%", "", $2); print $2}' "${report}" | tail -1
}

record "# WireGuard Through Gatherlink Scheduler Sweep"
record ""
record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
record "- duration_seconds: ${DURATION}"
record "- schedulers: ${SCHEDULERS}"
record "- raw_target_mbit: ${RAW_TARGET_MBIT}"
record "- payload_size: ${PAYLOAD_SIZE}"
record "- tcp_parallel: ${PARALLEL}"
record "- udp_rate: ${UDP_RATE}"
record "- udp_length: ${UDP_LENGTH}"
record "- link_mtu: ${LINK_MTU}"
record "- wg_mtu: ${WG_MTU}"
record "- path_mtu: ${PATH_MTU}"
record "- active_paths: ${ACTIVE_PATHS}"
record "- path_capacity_mbits: ${PATH_CAPACITY_MBITS}"
record "- flowlet_idle_us: ${FLOWLET_IDLE_US}"
record "- flowlet_max_hold_us: ${FLOWLET_MAX_HOLD_US}"
record "- path_run_datagrams: ${PATH_RUN_DATAGRAMS}"
record "- reorder_hold_us: ${REORDER_HOLD_US}"
record "- shape_profile: ${SHAPE_PROFILE}"
record ""

printf 'scheduler\traw_sink_mbit\traw_packet_delta\twg_tcp_mbit\twg_udp_mbit\twg_udp_loss_pct\n' >"${SUMMARY_TSV}"

IFS=',' read -r -a scheduler_list <<<"${SCHEDULERS}"
for scheduler in "${scheduler_list[@]}"; do
  scheduler="${scheduler//[[:space:]]/}"
  [[ -n "${scheduler}" ]] || continue

  raw_dir="${OUT_DIR}/raw-${scheduler}"
  wg_dir="${OUT_DIR}/wg-${scheduler}"
  record "## ${scheduler}"
  record ""

  "${SCRIPT_DIR}/run_gatherlink_onehop_speed.sh" \
    --ip "${IP}" \
    --port-a "${PORT_A}" \
    --port-b "${PORT_B}" \
    --duration "${DURATION}" \
    --payload-size "${PAYLOAD_SIZE}" \
    --target-mbit "${RAW_TARGET_MBIT}" \
    --link-mtu "${LINK_MTU}" \
    --path-mtu "${PATH_MTU}" \
    --core-batch-size "${CORE_BATCH_SIZE}" \
    --security-mode "${SECURITY_MODE}" \
    --active-paths "${ACTIVE_PATHS}" \
    --scheduler-mode "${scheduler}" \
    --path-capacity-mbits "${PATH_CAPACITY_MBITS}" \
    --flowlet-idle-us "${FLOWLET_IDLE_US}" \
    --flowlet-max-hold-us "${FLOWLET_MAX_HOLD_US}" \
    --path-run-datagrams "${PATH_RUN_DATAGRAMS}" \
    --reorder-hold-us "${REORDER_HOLD_US}" \
    --shape-profile "${SHAPE_PROFILE}" \
    --out "${raw_dir}" >"${raw_dir}.log"

  "${SCRIPT_DIR}/run_onehop_wireguard_gatherlink_speed.sh" \
    --ip "${IP}" \
    --port-a "${PORT_A}" \
    --port-b "${PORT_B}" \
    --duration "${DURATION}" \
    --parallel "${PARALLEL}" \
    --udp-rate "${UDP_RATE}" \
    --udp-length "${UDP_LENGTH}" \
    --link-mtu "${LINK_MTU}" \
    --wg-mtu "${WG_MTU}" \
    --path-mtu "${PATH_MTU}" \
    --core-batch-size "${CORE_BATCH_SIZE}" \
    --security-mode "${SECURITY_MODE}" \
    --active-paths "${ACTIVE_PATHS}" \
    --scheduler-mode "${scheduler}" \
    --path-capacity-mbits "${PATH_CAPACITY_MBITS}" \
    --flowlet-idle-us "${FLOWLET_IDLE_US}" \
    --flowlet-max-hold-us "${FLOWLET_MAX_HOLD_US}" \
    --path-run-datagrams "${PATH_RUN_DATAGRAMS}" \
    --reorder-hold-us "${REORDER_HOLD_US}" \
    --shape-profile "${SHAPE_PROFILE}" \
    --out "${wg_dir}" >"${wg_dir}.log"

  raw_sink_mbit="$(extract_raw_sink_mbit "${raw_dir}/report.md")"
  raw_delta="$(extract_raw_delta "${raw_dir}/report.md")"
  wg_tcp_mbit="$(extract_wg_tcp_mbit "${wg_dir}/report.md")"
  wg_udp_mbit="$(extract_wg_udp_mbit "${wg_dir}/report.md")"
  wg_udp_loss="$(extract_wg_udp_loss "${wg_dir}/report.md")"

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${scheduler}" \
    "${raw_sink_mbit:-unknown}" \
    "${raw_delta:-unknown}" \
    "${wg_tcp_mbit:-unknown}" \
    "${wg_udp_mbit:-unknown}" \
    "${wg_udp_loss:-unknown}" >>"${SUMMARY_TSV}"
  record "- raw_sink_mbit: ${raw_sink_mbit:-unknown}"
  record "- raw_packet_delta: ${raw_delta:-unknown}"
  record "- wg_tcp_mbit: ${wg_tcp_mbit:-unknown}"
  record "- wg_udp_mbit: ${wg_udp_mbit:-unknown}"
  record "- wg_udp_loss_pct: ${wg_udp_loss:-unknown}"
  record ""
done

record "## Summary"
record ""
record "| Scheduler | Raw GL sink Mbit/s | Raw packet delta | WG-over-GL TCP Mbit/s | WG-over-GL UDP Mbit/s | WG UDP loss % |"
record "| --- | ---: | ---: | ---: | ---: | ---: |"
tail -n +2 "${SUMMARY_TSV}" | while IFS=$'\t' read -r scheduler raw_sink raw_delta wg_tcp wg_udp wg_loss; do
  record "| \`${scheduler}\` | ${raw_sink} | ${raw_delta} | ${wg_tcp} | ${wg_udp} | ${wg_loss} |"
done

printf '\nWireGuard through Gatherlink scheduler sweep complete.\nReport: %s\n' "${REPORT}"
