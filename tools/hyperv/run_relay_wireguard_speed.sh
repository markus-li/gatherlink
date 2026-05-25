#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/perf_common.sh"

IP="172.22.0.1"
PORT_A="2201"
PORT_B="2202"
PORT_C="2203"
DURATION=20
PARALLEL=6
UDP_RATE="1000M"
UDP_LENGTH=1300
WG_MTU=1380
PATH_MTU=1472
PATH_CAPACITY_MBIT=5000
ACTIVE_PATHS="a,b,c"
FLOWLET_IDLE_US=50000
FLOWLET_MAX_HOLD_US=60000000
PATH_RUN_DATAGRAMS=0
REORDER_HOLD_US=2000
SCHEDULER_MODE="round_robin"
OUT_DIR="$(perf_repo_root)/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-relay-wireguard-speed"

usage() {
  cat <<'USAGE'
Usage: run_relay_wireguard_speed.sh [options]

Starts the production Gatherlink untrusted relay topology with VM C as transit,
then puts a real WireGuard interface over that Gatherlink UDP service and runs
iperf through WireGuard. This isolates "WireGuard over routed Gatherlink" from
raw Gatherlink and direct WireGuard baselines.

Options:
  --ip IP                    Management IP used with WSL portproxy. Default 172.22.0.1.
  --port-a PORT              SSH port for VM A. Default 2201.
  --port-b PORT              SSH port for VM B. Default 2202.
  --port-c PORT              SSH port for VM C. Default 2203.
  --duration SECONDS         iperf duration. Default 20.
  --parallel N               TCP parallel streams. Default 6.
  --udp-rate RATE            UDP offered rate. Default 1000M.
  --udp-length BYTES         UDP block size. Default 1300.
  --wg-mtu BYTES             WireGuard interface MTU. Default 1380.
  --path-mtu BYTES           Gatherlink path MTU. Default 1472.
  --path-capacity-mbit MBIT  Static per-path scheduler capacity hint. Default 5000.
  --active-paths LIST        Comma-separated a,b,c path list. Default a,b,c.
  --flowlet-idle-us N        Gatherlink service flowlet idle timeout. Default 50000.
  --flowlet-max-hold-us N    Gatherlink service max continuous hold. Default 60000000.
  --path-run-datagrams N     Maximum hot-burst datagrams per path before rescheduling. Default 0.
  --reorder-hold-us N        Gatherlink reorder hold. Default 2000.
  --scheduler-mode MODE      Gatherlink path scheduler mode. Default round_robin.
  --out DIR                  Report directory.
  --keep-running             Leave services and WireGuard interfaces running.
  --skip-kernel-tuning       Do not apply UDP socket-buffer sysctls first.
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
    --wg-mtu) WG_MTU="$2"; shift 2 ;;
    --path-mtu) PATH_MTU="$2"; shift 2 ;;
    --path-capacity-mbit) PATH_CAPACITY_MBIT="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --flowlet-idle-us) FLOWLET_IDLE_US="$2"; shift 2 ;;
    --flowlet-max-hold-us) FLOWLET_MAX_HOLD_US="$2"; shift 2 ;;
    --path-run-datagrams) PATH_RUN_DATAGRAMS="$2"; shift 2 ;;
    --reorder-hold-us) REORDER_HOLD_US="$2"; shift 2 ;;
    --scheduler-mode) SCHEDULER_MODE="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --keep-running) PERF_KEEP_RUNNING=1; shift ;;
    --skip-kernel-tuning) PERF_APPLY_KERNEL_TUNING=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

perf_init_defaults
REPORT="${OUT_DIR}/report.md"
: >"${REPORT}"
if [[ "${PERF_KEEP_RUNNING}" -eq 0 ]]; then
  trap perf_cleanup_all EXIT
fi

perf_record "# WireGuard Over Gatherlink Relay Speed"
perf_record ""
perf_record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
perf_record "- duration_seconds: ${DURATION}"
perf_record "- tcp_parallel: ${PARALLEL}"
perf_record "- udp_rate: ${UDP_RATE}"
perf_record "- udp_length: ${UDP_LENGTH}"
perf_record "- wg_mtu: ${WG_MTU}"
perf_record "- path_mtu: ${PATH_MTU}"
perf_record "- path_capacity_mbit: ${PATH_CAPACITY_MBIT}"
perf_record "- active_paths: ${ACTIVE_PATHS}"
perf_record "- flowlet_idle_us: ${FLOWLET_IDLE_US}"
perf_record "- flowlet_max_hold_us: ${FLOWLET_MAX_HOLD_US}"
perf_record "- path_run_datagrams: ${PATH_RUN_DATAGRAMS}"
perf_record "- reorder_hold_us: ${REORDER_HOLD_US}"
perf_record "- scheduler_mode: ${SCHEDULER_MODE}"
perf_record "- output: ${OUT_DIR}"
perf_record ""

perf_cleanup_all
if [[ "${PERF_APPLY_KERNEL_TUNING}" -eq 1 ]]; then
  perf_step "Kernel Tuning"
  perf_apply_kernel_tuning
  perf_record "Applied lab UDP socket-buffer tuning to VM A, VM B, and relay VM C."
fi

perf_step "Start Gatherlink Relay"
relay_tuning_arg=()
if [[ "${PERF_APPLY_KERNEL_TUNING}" -eq 0 ]]; then
  relay_tuning_arg=(--skip-kernel-tuning)
fi
"${SCRIPT_DIR}/run_relay_udp_speed.sh" \
  --ip "${IP}" \
  --port-a "${PORT_A}" \
  --port-b "${PORT_B}" \
  --port-c "${PORT_C}" \
  --duration "${DURATION}" \
  --payload-size 1350 \
  --path-mtu "${PATH_MTU}" \
  --path-capacity-mbit "${PATH_CAPACITY_MBIT}" \
  --target-mbit 1200 \
  --setup-only \
  --active-paths "${ACTIVE_PATHS}" \
  --scheduler-mode "${SCHEDULER_MODE}" \
  --flowlet-idle-us "${FLOWLET_IDLE_US}" \
  --flowlet-max-hold-us "${FLOWLET_MAX_HOLD_US}" \
  --path-run-datagrams "${PATH_RUN_DATAGRAMS}" \
  --reorder-hold-us "${REORDER_HOLD_US}" \
  --out "${OUT_DIR}/relay-setup" \
  "${relay_tuning_arg[@]}" | tee "${OUT_DIR}/relay-setup.log"

perf_step "WireGuard Setup"
perf_remote_a 'if [ ! -s "$HOME/wg-perf.key" ]; then umask 077; wg genkey > "$HOME/wg-perf.key"; fi; wg pubkey < "$HOME/wg-perf.key" > "$HOME/wg-perf.pub"'
perf_remote_b 'if [ ! -s "$HOME/wg-perf.key" ]; then umask 077; wg genkey > "$HOME/wg-perf.key"; fi; wg pubkey < "$HOME/wg-perf.key" > "$HOME/wg-perf.pub"'
WG_A_PUB="$(perf_remote_a 'cat "$HOME/wg-perf.pub"' | tr -d '\r\n')"
WG_B_PUB="$(perf_remote_b 'cat "$HOME/wg-perf.pub"' | tr -d '\r\n')"
perf_remote_a "sudo ip link del wg-gr-a 2>/dev/null || true; sudo ip link add wg-gr-a type wireguard; sudo ip addr add 10.203.0.1/24 dev wg-gr-a; sudo wg set wg-gr-a listen-port 19091 private-key /home/gatherlink/wg-perf.key peer '${WG_B_PUB}' allowed-ips 10.203.0.2/32; sudo ip link set wg-gr-a mtu ${WG_MTU} up"
perf_remote_b "sudo ip link del wg-gr-b 2>/dev/null || true; sudo ip link add wg-gr-b type wireguard; sudo ip addr add 10.203.0.2/24 dev wg-gr-b; sudo wg set wg-gr-b listen-port 19092 private-key /home/gatherlink/wg-perf.key peer '${WG_A_PUB}' allowed-ips 10.203.0.1/32 endpoint 127.0.0.1:55180 persistent-keepalive 5; sudo ip link set wg-gr-b mtu ${WG_MTU} up"
sleep 3
perf_remote_b "ping -c 3 -W 1 10.203.0.1" | tee "${OUT_DIR}/wg-ping.txt"

perf_step "Benchmarks"
probe_duration=$((DURATION * 2 + 12))
perf_start_node_probe "relaywg-node-a" "${PORT_A}" "${probe_duration}"
perf_start_node_probe "relaywg-node-b" "${PORT_B}" "${probe_duration}"
perf_start_node_probe "relaywg-node-c" "${PORT_C}" "${probe_duration}"
perf_run_iperf_tcp "wg-over-gl-relay-tcp" "${PORT_A}" "${PORT_B}" "10.203.0.1" "10.203.0.1" 7701 "${PARALLEL}" "${DURATION}"
perf_run_iperf_udp "wg-over-gl-relay-udp" "${PORT_A}" "${PORT_B}" "10.203.0.1" "10.203.0.1" 7702 "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
perf_remote_a "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status relayudp.vm.node-a" >"${OUT_DIR}/status-node-a.json" || true
perf_remote_b "cd /home/gatherlink/src/gatherlink && .venv/bin/gatherlink services status relayudp.vm.node-b" >"${OUT_DIR}/status-node-b.json" || true
perf_fetch_node_probe "relaywg-node-a" "${PORT_A}"
perf_fetch_node_probe "relaywg-node-b" "${PORT_B}"
perf_fetch_node_probe "relaywg-node-c" "${PORT_C}"

perf_step "Summary"
perf_summarize_iperf_jsons | tee -a "${REPORT}"
perf_record ""
perf_record "JSON summary: ${REPORT_JSON}"
if [[ "${PERF_KEEP_RUNNING}" -eq 1 ]]; then
  perf_record "Gatherlink relay services and WireGuard interfaces were left running."
fi
printf '\nWireGuard over Gatherlink relay speed complete.\nReport: %s\n' "${REPORT}"
