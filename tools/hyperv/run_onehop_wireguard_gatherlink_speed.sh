#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/perf_common.sh"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
DURATION=20
PARALLEL=24
UDP_RATE="1000M"
UDP_LENGTH=1300
LINK_MTU=1500
WG_MTU=1380
PATH_MTU=1472
CORE_BATCH_SIZE=512
SCHEDULER_REAPPLY_INTERVAL=""
SECURITY_MODE="authenticated"
ACTIVE_PATHS="a,b,c"
SCHEDULER_MODE="coordinated_adaptive"
SCHEDULER_TRAFFIC_BIAS="auto"
SERVICE_TRAFFIC_CLASS="tcp_ordered"
PATH_CAPACITY_MBITS=""
FLOWLET_IDLE_US=0
FLOWLET_MAX_HOLD_US=0
PATH_RUN_DATAGRAMS=0
REORDER_HOLD_US=2000
SHAPE_PROFILE="clean"
RUN_TCP=1
RUN_UDP=1
RUN_MIXED=0
OUT_DIR="$(perf_repo_root)/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-onehop-wireguard-gatherlink-speed"

usage() {
  cat <<'USAGE'
Usage: run_onehop_wireguard_gatherlink_speed.sh [options]

Starts direct two-node Gatherlink carrier sockets, then puts a real WireGuard
interface over that Gatherlink UDP service and runs iperf through WireGuard.
This isolates endpoint Gatherlink transport cost from untrusted relay forwarding.

Options:
  --ip IP                  Management IP used with WSL portproxy. Default 172.22.0.1.
  --port-a PORT            SSH port for VM A. Default 2201.
  --port-b PORT            SSH port for VM B. Default 2202.
  --duration SECONDS       iperf duration. Default 20.
  --parallel N             TCP parallel streams. Default 24.
  --udp-rate RATE          UDP offered rate. Default 1000M.
  --udp-length BYTES       UDP block size. Default 1300.
  --link-mtu BYTES         Linux path interface MTU on VM A and VM B. Default 1500.
  --wg-mtu BYTES           WireGuard interface MTU. Default 1380.
  --path-mtu BYTES         Gatherlink path MTU. Default 1472.
  --core-batch-size N      Core runner batch size passed to gatherlink run start. Default 512.
  --scheduler-reapply-interval SECONDS
                           Enable live Python scheduler reapply in the underlying Gatherlink services.
                           Default disabled.
  --security-mode MODE     Gatherlink security mode: authenticated or none. Default authenticated.
  --active-paths LIST      Comma-separated a,b,c,d,e path list. Default a,b,c.
  --scheduler-mode MODE    Python-selected Gatherlink scheduler. Default coordinated_adaptive.
  --scheduler-traffic-bias BIAS
                          Bias coordinated_adaptive toward auto, tcp, or udp. Default auto.
  --service-traffic-class CLASS
                          Traffic class for the generated Gatherlink service. Default tcp_ordered because a
                          single WireGuard tunnel is opaque/order-sensitive even when it carries UDP.
  --path-capacity-mbits SPEC
                          Static per-path scheduler hints, for example a:300,b:500,c:700.
                          If omitted, the raw Gatherlink runner uses its default path hints.
  --flowlet-idle-us N      Gatherlink service flowlet idle timeout. Default 0.
  --flowlet-max-hold-us N  Gatherlink service max continuous hold. Default 0.
  --path-run-datagrams N   Maximum hot-burst datagrams per path before rescheduling. Default 0.
  --reorder-hold-us N      Gatherlink reorder hold. Default 2000.
  --shape-profile NAME     Hyper-V path shaping profile. Default clean.
  --out DIR                Report directory.
  --keep-running           Leave services and WireGuard interfaces running.
  --skip-kernel-tuning     Do not apply UDP socket-buffer sysctls first.
  --tcp-only               Run only the TCP benchmark after tunnel setup.
  --udp-only               Run only the UDP benchmark after tunnel setup.
  --mixed                  Run TCP and UDP concurrently instead of sequentially.
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
    --link-mtu) LINK_MTU="$2"; shift 2 ;;
    --wg-mtu) WG_MTU="$2"; shift 2 ;;
    --path-mtu) PATH_MTU="$2"; shift 2 ;;
    --core-batch-size) CORE_BATCH_SIZE="$2"; shift 2 ;;
    --scheduler-reapply-interval) SCHEDULER_REAPPLY_INTERVAL="$2"; shift 2 ;;
    --security-mode) SECURITY_MODE="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --scheduler-mode) SCHEDULER_MODE="$2"; shift 2 ;;
    --scheduler-traffic-bias) SCHEDULER_TRAFFIC_BIAS="$2"; shift 2 ;;
    --service-traffic-class) SERVICE_TRAFFIC_CLASS="$2"; shift 2 ;;
    --path-capacity-mbits) PATH_CAPACITY_MBITS="$2"; shift 2 ;;
    --flowlet-idle-us) FLOWLET_IDLE_US="$2"; shift 2 ;;
    --flowlet-max-hold-us) FLOWLET_MAX_HOLD_US="$2"; shift 2 ;;
    --path-run-datagrams) PATH_RUN_DATAGRAMS="$2"; shift 2 ;;
    --reorder-hold-us) REORDER_HOLD_US="$2"; shift 2 ;;
    --shape-profile) SHAPE_PROFILE="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --keep-running) PERF_KEEP_RUNNING=1; shift ;;
    --skip-kernel-tuning) PERF_APPLY_KERNEL_TUNING=0; shift ;;
    --tcp-only) RUN_TCP=1; RUN_UDP=0; shift ;;
    --udp-only) RUN_TCP=0; RUN_UDP=1; shift ;;
    --mixed) RUN_MIXED=1; RUN_TCP=1; RUN_UDP=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "${RUN_TCP}" -eq 0 && "${RUN_UDP}" -eq 0 ]]; then
  echo "at least one benchmark mode must be enabled" >&2
  exit 2
fi

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

perf_init_defaults
REPORT="${OUT_DIR}/report.md"
: >"${REPORT}"
if [[ "${PERF_KEEP_RUNNING}" -eq 0 ]]; then
  trap perf_cleanup_all EXIT
fi

perf_record "# WireGuard Over Gatherlink One-Hop Speed"
perf_record ""
perf_record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
perf_record "- duration_seconds: ${DURATION}"
perf_record "- tcp_parallel: ${PARALLEL}"
perf_record "- udp_rate: ${UDP_RATE}"
perf_record "- udp_length: ${UDP_LENGTH}"
perf_record "- link_mtu: ${LINK_MTU}"
perf_record "- wg_mtu: ${WG_MTU}"
perf_record "- path_mtu: ${PATH_MTU}"
perf_record "- core_batch_size: ${CORE_BATCH_SIZE}"
perf_record "- scheduler_reapply_interval: ${SCHEDULER_REAPPLY_INTERVAL:-disabled}"
perf_record "- security_mode: ${SECURITY_MODE}"
perf_record "- active_paths: ${ACTIVE_PATHS}"
perf_record "- scheduler_mode: ${SCHEDULER_MODE}"
perf_record "- scheduler_traffic_bias: ${SCHEDULER_TRAFFIC_BIAS}"
perf_record "- service_traffic_class: ${SERVICE_TRAFFIC_CLASS}"
if [[ "${SCHEDULER_TRAFFIC_BIAS}" == "udp" && "${SERVICE_TRAFFIC_CLASS}" == "tcp_ordered" ]]; then
  perf_record "- scheduler_warning: udp bias can stripe an opaque WireGuard tunnel and trigger WireGuard anti-replay loss"
fi
perf_record "- iperf_tcp_client_args: ${PERF_IPERF_TCP_CLIENT_ARGS:-[none]}"
perf_record "- iperf_tcp_server_args: ${PERF_IPERF_TCP_SERVER_ARGS:-[none]}"
perf_record "- path_capacity_mbits: ${PATH_CAPACITY_MBITS:-raw-runner-default}"
perf_record "- flowlet_idle_us: ${FLOWLET_IDLE_US}"
perf_record "- flowlet_max_hold_us: ${FLOWLET_MAX_HOLD_US}"
perf_record "- path_run_datagrams: ${PATH_RUN_DATAGRAMS}"
perf_record "- reorder_hold_us: ${REORDER_HOLD_US}"
perf_record "- shape_profile: ${SHAPE_PROFILE}"
perf_record "- run_tcp: ${RUN_TCP}"
perf_record "- run_udp: ${RUN_UDP}"
perf_record "- run_mixed: ${RUN_MIXED}"
perf_record "- output: ${OUT_DIR}"
perf_record ""

perf_cleanup_all
if [[ "${PERF_APPLY_KERNEL_TUNING}" -eq 1 ]]; then
  perf_step "Kernel Tuning"
  perf_apply_kernel_tuning
  perf_record "Applied lab UDP socket-buffer tuning to VM A and VM B."
fi

perf_step "Start Gatherlink One-Hop"
onehop_tuning_arg=()
if [[ "${PERF_APPLY_KERNEL_TUNING}" -eq 0 ]]; then
  onehop_tuning_arg=(--skip-kernel-tuning)
fi
path_capacity_arg=()
if [[ -n "${PATH_CAPACITY_MBITS}" ]]; then
  path_capacity_arg=(--path-capacity-mbits "${PATH_CAPACITY_MBITS}")
fi
scheduler_reapply_arg=()
if [[ -n "${SCHEDULER_REAPPLY_INTERVAL}" ]]; then
  scheduler_reapply_arg=(--scheduler-reapply-interval "${SCHEDULER_REAPPLY_INTERVAL}")
fi
"${SCRIPT_DIR}/run_gatherlink_onehop_speed.sh" \
  --ip "${IP}" \
  --port-a "${PORT_A}" \
  --port-b "${PORT_B}" \
  --duration "${DURATION}" \
  --payload-size 1350 \
  --link-mtu "${LINK_MTU}" \
  --path-mtu "${PATH_MTU}" \
  --core-batch-size "${CORE_BATCH_SIZE}" \
  "${scheduler_reapply_arg[@]}" \
  --security-mode "${SECURITY_MODE}" \
  --setup-only \
  --keep-running \
  --active-paths "${ACTIVE_PATHS}" \
  --scheduler-mode "${SCHEDULER_MODE}" \
  --scheduler-traffic-bias "${SCHEDULER_TRAFFIC_BIAS}" \
  --service-traffic-class "${SERVICE_TRAFFIC_CLASS}" \
  "${path_capacity_arg[@]}" \
  --flowlet-idle-us "${FLOWLET_IDLE_US}" \
  --flowlet-max-hold-us "${FLOWLET_MAX_HOLD_US}" \
  --path-run-datagrams "${PATH_RUN_DATAGRAMS}" \
  --reorder-hold-us "${REORDER_HOLD_US}" \
  --shape-profile "${SHAPE_PROFILE}" \
  --out "${OUT_DIR}/onehop-setup" \
  "${onehop_tuning_arg[@]}" | tee "${OUT_DIR}/onehop-setup.log"

perf_step "WireGuard Setup"
perf_remote_a 'if [ ! -s "$HOME/wg-perf.key" ]; then umask 077; wg genkey > "$HOME/wg-perf.key"; fi; wg pubkey < "$HOME/wg-perf.key" > "$HOME/wg-perf.pub"'
perf_remote_b 'if [ ! -s "$HOME/wg-perf.key" ]; then umask 077; wg genkey > "$HOME/wg-perf.key"; fi; wg pubkey < "$HOME/wg-perf.key" > "$HOME/wg-perf.pub"'
WG_A_PUB="$(perf_remote_a 'cat "$HOME/wg-perf.pub"' | tr -d '\r\n')"
WG_B_PUB="$(perf_remote_b 'cat "$HOME/wg-perf.pub"' | tr -d '\r\n')"
perf_remote_a "sudo ip link del wg-go-a 2>/dev/null || true; sudo ip link add wg-go-a type wireguard; sudo ip addr add 10.204.0.1/24 dev wg-go-a; sudo wg set wg-go-a listen-port 19091 private-key /home/gatherlink/wg-perf.key peer '${WG_B_PUB}' allowed-ips 10.204.0.2/32; sudo ip link set wg-go-a mtu ${WG_MTU} up"
perf_remote_b "sudo ip link del wg-go-b 2>/dev/null || true; sudo ip link add wg-go-b type wireguard; sudo ip addr add 10.204.0.2/24 dev wg-go-b; sudo wg set wg-go-b listen-port 19092 private-key /home/gatherlink/wg-perf.key peer '${WG_A_PUB}' allowed-ips 10.204.0.1/32 endpoint 127.0.0.1:55180 persistent-keepalive 5; sudo ip link set wg-go-b mtu ${WG_MTU} up"
sleep 3
perf_remote_b "ping -c 3 -W 1 10.204.0.1" | tee "${OUT_DIR}/wg-ping.txt"
if [[ "${RUN_UDP}" -eq 1 ]]; then
  perf_compile_udp_pressure "${PORT_A}"
  perf_compile_udp_pressure "${PORT_B}"
fi

perf_step "Benchmarks"
benchmark_sections=$((RUN_TCP + RUN_UDP))
if [[ "${RUN_MIXED}" -eq 1 ]]; then
  benchmark_sections=1
fi
# Keep the probe bounded to the benchmark window plus a small settle margin.
# A longer probe can still be running when the script fetches results, which
# leaves empty perf JSON files and hides path-split evidence during tuning.
probe_duration=$((DURATION * benchmark_sections + 2))
perf_start_node_probe "onehopwg-node-a" "${PORT_A}" "${probe_duration}"
perf_start_node_probe "onehopwg-node-b" "${PORT_B}" "${probe_duration}"
if [[ "${RUN_MIXED}" -eq 1 ]]; then
  perf_remote_a "pkill -x iperf3 2>/dev/null || true"
  perf_remote_b "pkill -x iperf3 2>/dev/null || true"
  perf_start_iperf_tcp_server "wg-over-gl-onehop-mixed-tcp" "${PORT_A}" "10.204.0.1" 7801
  perf_start_iperf_udp_server "wg-over-gl-onehop-mixed-udp" "${PORT_A}" "10.204.0.1" 7802
  sleep 1
  perf_start_iperf_tcp_client_background "wg-over-gl-onehop-mixed-tcp" "${PORT_B}" "10.204.0.1" 7801 "${PARALLEL}" "${DURATION}"
  perf_start_iperf_udp_client_background "wg-over-gl-onehop-mixed-udp" "${PORT_B}" "10.204.0.1" 7802 "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
  sleep $((DURATION + 2))
  perf_fetch_iperf_tcp_background "wg-over-gl-onehop-mixed-tcp" "${PORT_A}" "${PORT_B}"
  perf_fetch_iperf_udp_background "wg-over-gl-onehop-mixed-udp" "${PORT_A}" "${PORT_B}"
elif [[ "${RUN_TCP}" -eq 1 ]]; then
  perf_run_iperf_tcp "wg-over-gl-onehop-tcp" "${PORT_A}" "${PORT_B}" "10.204.0.1" "10.204.0.1" 7801 "${PARALLEL}" "${DURATION}"
fi
if [[ "${RUN_MIXED}" -eq 0 && "${RUN_UDP}" -eq 1 ]]; then
  perf_run_iperf_udp "wg-over-gl-onehop-udp" "${PORT_A}" "${PORT_B}" "10.204.0.1" "10.204.0.1" 7802 "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
  perf_step "UDP Pressure"
  pressure_mbit="$(udp_rate_mbit)"
  perf_start_udp_pressure_sink \
    "wg-over-gl-onehop-udp-pressure" \
    "${PORT_A}" \
    "10.204.0.1:8300" \
    "${DURATION}" \
    "10.204.0.2:8400"
  sleep 1
  perf_start_udp_pressure_client_background \
    "wg-over-gl-onehop-udp-pressure" \
    "${PORT_B}" \
    "10.204.0.1:8300" \
    "${DURATION}" \
    "${UDP_LENGTH}" \
    "${pressure_mbit}" \
    "10.204.0.2:8400"
  sleep $((DURATION + 3))
  perf_fetch_udp_pressure_background "wg-over-gl-onehop-udp-pressure" "${PORT_A}" "${PORT_B}"
fi
perf_remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status gl-onehop.vm.node-a" >"${OUT_DIR}/status-node-a.json" || true
perf_remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status gl-onehop.vm.node-b" >"${OUT_DIR}/status-node-b.json" || true
perf_fetch_node_probe "onehopwg-node-a" "${PORT_A}"
perf_fetch_node_probe "onehopwg-node-b" "${PORT_B}"

perf_step "Summary"
perf_summarize_iperf_jsons | tee -a "${REPORT}"
perf_record ""
perf_record "JSON summary: ${REPORT_JSON}"
if [[ "${PERF_KEEP_RUNNING}" -eq 1 ]]; then
  perf_record "Gatherlink services and WireGuard interfaces were left running."
fi
printf '\nWireGuard over Gatherlink one-hop speed complete.\nReport: %s\n' "${REPORT}"
