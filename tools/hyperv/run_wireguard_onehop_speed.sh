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
UDP_PAYLOAD_MARGIN=0
LINK_MTU=1500
WG_MTU=1360
IMPLEMENTATION="kernel"
ACTIVE_PATHS="a,b,c"
UDP_PRESSURE_UNBOUNDED=0
SHAPE_PROFILE="clean"
OUT_DIR="$(perf_repo_root)/.gatherlink/hyperv-performance/$(date -u +%Y%m%dT%H%M%SZ)-wireguard-onehop-${IMPLEMENTATION}"

usage() {
  cat <<'USAGE'
Usage: run_wireguard_onehop_speed.sh [options]

Runs direct one-hop WireGuard between VM B and VM A over the private path
interfaces. Use --implementation userspace to run wireguard-go, gotatun to run
Mullvad's optional Rust userspace backend, or boringtun to run Cloudflare's
optional Rust userspace backend, instead of the Linux kernel WireGuard netdev.

Options:
  --implementation MODE kernel, userspace, gotatun, or boringtun. Default kernel.
  --ip IP              Management IP used with WSL portproxy. Default 172.22.0.1.
  --port-a PORT        SSH port for VM A. Default 2201.
  --port-b PORT        SSH port for VM B. Default 2202.
  --duration SECONDS   iperf duration. Default 20.
  --parallel N         TCP parallel streams. Default 6.
  --udp-rate RATE      UDP offered rate per path. Default 1000M.
  --udp-length BYTES   UDP block size, or auto to derive from WG MTU. Default 1200.
  --udp-payload-margin BYTES
                        Extra bytes to subtract when --udp-length auto is used.
                        Use this for smaller paths or extra encapsulation overhead.
  --link-mtu BYTES     Linux path interface MTU on VM A and VM B. Default 1500.
  --wg-mtu BYTES       WireGuard interface MTU. Default 1360.
  --active-paths LIST  Comma-separated path letters. Default a,b,c; supports a,b,c,d,e.
  --shape-profile NAME Hyper-V path shaping profile. Default clean.
  --udp-pressure-unbounded
                        Run udp_pressure over WireGuard without pacing in the simultaneous section.
  --out DIR            Report directory.
  --skip-kernel-tuning Do not apply UDP socket-buffer sysctls first.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --implementation) IMPLEMENTATION="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --port-a) PORT_A="$2"; shift 2 ;;
    --port-b) PORT_B="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --udp-rate) UDP_RATE="$2"; shift 2 ;;
    --udp-length) UDP_LENGTH="$2"; shift 2 ;;
    --udp-payload-margin) UDP_PAYLOAD_MARGIN="$2"; shift 2 ;;
    --link-mtu) LINK_MTU="$2"; shift 2 ;;
    --wg-mtu) WG_MTU="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --shape-profile) SHAPE_PROFILE="$2"; shift 2 ;;
    --udp-pressure-unbounded) UDP_PRESSURE_UNBOUNDED=1; shift ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --skip-kernel-tuning) PERF_APPLY_KERNEL_TUNING=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "${IMPLEMENTATION}" in
  kernel|userspace|gotatun|boringtun) ;;
  *) echo "--implementation must be kernel, userspace, gotatun, or boringtun" >&2; exit 2 ;;
esac

perf_init_defaults

resolve_udp_length() {
  python3 - "$UDP_LENGTH" "$WG_MTU" "$UDP_PAYLOAD_MARGIN" <<'PY'
import sys

value = sys.argv[1]
wg_mtu = int(sys.argv[2])
margin = int(sys.argv[3])
if margin < 0:
    raise SystemExit("--udp-payload-margin must be zero or greater")
if value == "auto":
    # UDP payload carried inside the WireGuard interface. IPv4 UDP overhead is
    # 20 byte IP header plus 8 byte UDP header; explicit margin covers extra
    # lab or transport overhead when the path is tighter than the WG MTU says.
    resolved = wg_mtu - 28 - margin
else:
    resolved = int(value)
if resolved <= 0:
    raise SystemExit("resolved UDP payload length must be greater than zero")
print(resolved)
PY
}

UDP_LENGTH="$(resolve_udp_length)"
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

wg_iface() {
  local index="$1"
  if [[ "${IMPLEMENTATION}" == "kernel" ]]; then
    printf 'wgk%s' "${index}"
  elif [[ "${IMPLEMENTATION}" == "userspace" ]]; then
    printf 'wgu%s' "${index}"
  elif [[ "${IMPLEMENTATION}" == "boringtun" ]]; then
    printf 'wgb%s' "${index}"
  else
    printf 'wgr%s' "${index}"
  fi
}

create_iface_command() {
  local iface="$1"
  if [[ "${IMPLEMENTATION}" == "kernel" ]]; then
    printf "sudo ip link add %q type wireguard" "${iface}"
  elif [[ "${IMPLEMENTATION}" == "userspace" ]]; then
    printf "sudo env WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 wireguard-go %q" "${iface}"
  elif [[ "${IMPLEMENTATION}" == "boringtun" ]]; then
    printf "sudo boringtun-cli %q" "${iface}"
  else
    printf "sudo gotatun %q" "${iface}"
  fi
}

setup_path() {
  local index="$1"
  local iface
  iface="$(wg_iface "${index}")"
  local b_ip="10.204.${index}.1"
  local a_ip="10.204.${index}.2"
  local b_underlay="10.91.${index}.12"
  local a_underlay="10.91.${index}.11"
  local b_port="$((7800 + index))"
  local a_port="$((7900 + index))"

  perf_remote_a "wg genkey | tee /tmp/${iface}-a.key | wg pubkey > /tmp/${iface}-a.pub"
  perf_remote_b "wg genkey | tee /tmp/${iface}-b.key | wg pubkey > /tmp/${iface}-b.pub"
  local a_pub b_pub
  a_pub="$(perf_remote_a "cat /tmp/${iface}-a.pub" | tr -d '\r\n')"
  b_pub="$(perf_remote_b "cat /tmp/${iface}-b.pub" | tr -d '\r\n')"

  perf_remote_a "sudo ip link del ${iface} 2>/dev/null || true; $(create_iface_command "${iface}"); sleep 0.5; sudo ip addr add ${a_ip}/30 dev ${iface}; sudo wg set ${iface} listen-port ${a_port} private-key /tmp/${iface}-a.key peer '${b_pub}' allowed-ips ${b_ip}/32 endpoint ${b_underlay}:${b_port} persistent-keepalive 5; sudo ip link set ${iface} mtu ${WG_MTU} up"
  perf_remote_b "sudo ip link del ${iface} 2>/dev/null || true; $(create_iface_command "${iface}"); sleep 0.5; sudo ip addr add ${b_ip}/30 dev ${iface}; sudo wg set ${iface} listen-port ${b_port} private-key /tmp/${iface}-b.key peer '${a_pub}' allowed-ips ${a_ip}/32 endpoint ${a_underlay}:${a_port} persistent-keepalive 5; sudo ip link set ${iface} mtu ${WG_MTU} up"
}

apply_link_mtu() {
  local index="$1"
  local letter
  letter="$(printf '%b' "\\$(printf '%03o' "$((96 + index))")")"
  perf_remote_a "sudo ip link set path-${letter} mtu ${LINK_MTU} up"
  perf_remote_b "sudo ip link set path-${letter} mtu ${LINK_MTU} up"
}

perf_record "# WireGuard One-Hop Speed"
perf_record ""
perf_record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
perf_record "- implementation: ${IMPLEMENTATION}"
perf_record "- duration_seconds: ${DURATION}"
perf_record "- tcp_parallel: ${PARALLEL}"
perf_record "- udp_rate: ${UDP_RATE}"
perf_record "- udp_length: ${UDP_LENGTH}"
perf_record "- udp_payload_margin: ${UDP_PAYLOAD_MARGIN}"
perf_record "- link_mtu: ${LINK_MTU}"
perf_record "- wg_mtu: ${WG_MTU}"
perf_record "- active_paths: ${ACTIVE_PATHS}"
perf_record "- shape_profile: ${SHAPE_PROFILE}"
perf_record "- udp_pressure_unbounded: ${UDP_PRESSURE_UNBOUNDED}"
perf_record "- output: ${OUT_DIR}"
perf_record ""

perf_cleanup_all
if [[ "${PERF_APPLY_KERNEL_TUNING}" -eq 1 ]]; then
  perf_step "Kernel Tuning"
  perf_apply_kernel_tuning
  perf_record "Applied lab UDP socket-buffer tuning."
fi

if [[ "${IMPLEMENTATION}" == "userspace" ]]; then
  perf_remote_a "command -v wireguard-go"
  perf_remote_b "command -v wireguard-go"
elif [[ "${IMPLEMENTATION}" == "gotatun" ]]; then
  perf_remote_a "command -v gotatun"
  perf_remote_b "command -v gotatun"
elif [[ "${IMPLEMENTATION}" == "boringtun" ]]; then
  perf_remote_a "command -v boringtun-cli"
  perf_remote_b "command -v boringtun-cli"
fi
perf_compile_udp_pressure "${PORT_A}"
perf_compile_udp_pressure "${PORT_B}"

perf_step "Setup"
"${SCRIPT_DIR}/apply_path_shape_profile.sh" \
  --ip "${IP}" \
  --ports "${PORT_A},${PORT_B}" \
  --active-paths "${ACTIVE_PATHS}" \
  --profile "${SHAPE_PROFILE}" \
  --link-mtu "${LINK_MTU}"
for index in $(path_indexes); do
  setup_path "${index}"
done
sleep 2

perf_step "Reachability"
for index in $(path_indexes); do
  perf_remote_b "for attempt in 1 2 3 4 5; do ping -c 2 -W 1 10.204.${index}.2 && exit 0; sleep 1; done; exit 1" \
    | tee "${OUT_DIR}/path-${index}-ping.txt"
done

perf_step "Benchmarks"
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}"
  perf_run_iperf_tcp "${label}-tcp" "${PORT_A}" "${PORT_B}" "10.204.${index}.2" "10.204.${index}.2" "$((7800 + index + 100))" "${PARALLEL}" "${DURATION}"
  perf_run_iperf_udp "${label}-udp" "${PORT_A}" "${PORT_B}" "10.204.${index}.2" "10.204.${index}.2" "$((7800 + index + 200))" "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
done

perf_step "Simultaneous TCP"
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-tcp-simultaneous"
  perf_start_iperf_tcp_server "${label}" "${PORT_A}" "10.204.${index}.2" "$((8000 + index))"
done
sleep 1
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-tcp-simultaneous"
  perf_start_iperf_tcp_client_background "${label}" "${PORT_B}" "10.204.${index}.2" "$((8000 + index))" "${PARALLEL}" "${DURATION}"
done
sleep "$((DURATION + 3))"
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-tcp-simultaneous"
  perf_fetch_iperf_tcp_background "${label}" "${PORT_A}" "${PORT_B}"
done

perf_step "Simultaneous UDP"
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-udp-simultaneous"
  perf_start_iperf_udp_server "${label}" "${PORT_A}" "10.204.${index}.2" "$((8100 + index))"
done
sleep 1
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-udp-simultaneous"
  perf_start_iperf_udp_client_background "${label}" "${PORT_B}" "10.204.${index}.2" "$((8100 + index))" "${UDP_RATE}" "${UDP_LENGTH}" "${DURATION}"
done
sleep "$((DURATION + 3))"
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-udp-simultaneous"
  perf_fetch_iperf_udp_background "${label}" "${PORT_A}" "${PORT_B}"
done

perf_step "UDP Pressure Simultaneous"
pressure_mbit="$(udp_rate_mbit)"
if [[ "${UDP_PRESSURE_UNBOUNDED}" -eq 1 ]]; then
  pressure_mbit=""
fi
if [[ "${PERF_COLLECT_NODE_PROBES}" -eq 1 ]]; then
  perf_start_node_probe "udp-pressure-node-a" "${PORT_A}" "$((DURATION + 5))"
  perf_start_node_probe "udp-pressure-node-b" "${PORT_B}" "$((DURATION + 5))"
fi
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-udp-pressure-simultaneous"
  perf_start_udp_pressure_sink \
    "${label}" \
    "${PORT_A}" \
    "10.204.${index}.2:$((8200 + index * 100))" \
    "${DURATION}" \
    "10.204.${index}.1:$((8300 + index * 100))"
done
sleep 1
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-udp-pressure-simultaneous"
  perf_start_udp_pressure_client_background \
    "${label}" \
    "${PORT_B}" \
    "10.204.${index}.2:$((8200 + index * 100))" \
    "${DURATION}" \
    "${UDP_LENGTH}" \
    "${pressure_mbit}" \
    "10.204.${index}.1:$((8300 + index * 100))"
done
sleep "$((DURATION + 3))"
for index in $(path_indexes); do
  label="wg-${IMPLEMENTATION}-path-${index}-udp-pressure-simultaneous"
  perf_fetch_udp_pressure_background "${label}" "${PORT_A}" "${PORT_B}"
done
if [[ "${PERF_COLLECT_NODE_PROBES}" -eq 1 ]]; then
  perf_fetch_node_probe "udp-pressure-node-a" "${PORT_A}"
  perf_fetch_node_probe "udp-pressure-node-b" "${PORT_B}"
fi

perf_step "Summary"
perf_summarize_iperf_jsons | tee -a "${REPORT}"
perf_record ""
perf_record "JSON summary: ${REPORT_JSON}"
printf '\nWireGuard one-hop speed complete.\nReport: %s\n' "${REPORT}"
