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
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-routing-speed/$(date -u +%Y%m%dT%H%M%SZ)-direct-wireguard"
KEEP_RUNNING=0

usage() {
  cat <<'USAGE'
Usage: run_direct_wireguard_routing_speed.sh [options]

Builds a temporary direct WireGuard routing baseline across the three Hyper-V
VMs:

  VM B -> three WireGuard links -> VM C -> three WireGuard links -> VM A

The script uses the existing path-a/path-b/path-c lab networks and never uses
the VM management LAN for data traffic. It runs per-path hop tests and an
end-to-end B-to-A test through C.

Options:
  --ip IP              Management IP used with the WSL portproxy setup. Default 172.22.0.1.
  --port-a PORT        SSH port for VM A. Default 2201.
  --port-b PORT        SSH port for VM B. Default 2202.
  --port-c PORT        SSH port for VM C. Default 2203.
  --duration SECONDS   iperf duration per run. Default 20.
  --parallel N         iperf parallel streams for end-to-end run. Default 6.
  --out DIR            Report directory.
  --keep-running       Leave temporary WireGuard interfaces up for inspection.
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

remote() {
  local port="$1"
  local command="$2"
  ssh -n -p "${port}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new gatherlink@"${IP}" "${command}"
}

remote_a() { remote "${PORT_A}" "$1"; }
remote_b() { remote "${PORT_B}" "$1"; }
remote_c() { remote "${PORT_C}" "$1"; }

cleanup() {
  for port in "${PORT_A}" "${PORT_B}" "${PORT_C}"; do
    remote "${port}" '
      for dev in wg-bc-a wg-bc-b wg-bc-c wg-ca-a wg-ca-b wg-ca-c; do
        sudo ip link del "${dev}" 2>/dev/null || true
      done
      sudo ip addr del 10.250.100.1/32 dev lo 2>/dev/null || true
      sudo ip addr del 10.250.200.1/32 dev lo 2>/dev/null || true
      pkill -x iperf3 2>/dev/null || true
      for path in path-a path-b path-c; do
        sudo tc qdisc del dev "${path}" root 2>/dev/null || true
        sudo ip link set "${path}" up 2>/dev/null || true
      done
    ' >/dev/null 2>&1 || true
  done
}

if [[ "${KEEP_RUNNING}" -eq 0 ]]; then
  trap cleanup EXIT
fi

get_pub() {
  local port="$1"
  local name="$2"
  remote "${port}" "cat /tmp/wg-speed-${name}.pub" | tr -d '\r\n'
}

prepare_node() {
  local port="$1"
  remote "${port}" '
    for path in path-a path-b path-c; do
      sudo tc qdisc del dev "${path}" root 2>/dev/null || true
      sudo ip link set "${path}" up
    done
    command -v iperf3 >/dev/null
    command -v wg >/dev/null
    sudo sysctl -w net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 >/dev/null
    rm -f /tmp/wg-speed-*.key /tmp/wg-speed-*.pub
    for name in bc-a bc-b bc-c ca-a ca-b ca-c; do
      wg genkey | tee "/tmp/wg-speed-${name}.key" | wg pubkey >"/tmp/wg-speed-${name}.pub"
    done
  '
}

setup_wireguard() {
  cleanup
  prepare_node "${PORT_A}"
  prepare_node "${PORT_B}"
  prepare_node "${PORT_C}"
  remote_c 'sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null'

  local b_bc_a b_bc_b b_bc_c c_bc_a c_bc_b c_bc_c c_ca_a c_ca_b c_ca_c a_ca_a a_ca_b a_ca_c
  b_bc_a="$(get_pub "${PORT_B}" bc-a)"
  b_bc_b="$(get_pub "${PORT_B}" bc-b)"
  b_bc_c="$(get_pub "${PORT_B}" bc-c)"
  c_bc_a="$(get_pub "${PORT_C}" bc-a)"
  c_bc_b="$(get_pub "${PORT_C}" bc-b)"
  c_bc_c="$(get_pub "${PORT_C}" bc-c)"
  c_ca_a="$(get_pub "${PORT_C}" ca-a)"
  c_ca_b="$(get_pub "${PORT_C}" ca-b)"
  c_ca_c="$(get_pub "${PORT_C}" ca-c)"
  a_ca_a="$(get_pub "${PORT_A}" ca-a)"
  a_ca_b="$(get_pub "${PORT_A}" ca-b)"
  a_ca_c="$(get_pub "${PORT_A}" ca-c)"

  remote_b "
    sudo ip addr add 10.250.100.1/32 dev lo 2>/dev/null || true
    sudo ip link add wg-bc-a type wireguard
    sudo ip addr add 10.250.1.1/30 dev wg-bc-a
    sudo wg set wg-bc-a private-key /tmp/wg-speed-bc-a.key listen-port 58001 peer '${c_bc_a}' allowed-ips 0.0.0.0/0 endpoint 10.91.1.13:58101 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-bc-a
    sudo ip link add wg-bc-b type wireguard
    sudo ip addr add 10.250.2.1/30 dev wg-bc-b
    sudo wg set wg-bc-b private-key /tmp/wg-speed-bc-b.key listen-port 58002 peer '${c_bc_b}' allowed-ips 0.0.0.0/0 endpoint 10.91.2.13:58102 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-bc-b
    sudo ip link add wg-bc-c type wireguard
    sudo ip addr add 10.250.3.1/30 dev wg-bc-c
    sudo wg set wg-bc-c private-key /tmp/wg-speed-bc-c.key listen-port 58003 peer '${c_bc_c}' allowed-ips 0.0.0.0/0 endpoint 10.91.3.13:58103 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-bc-c
    sudo ip route replace 10.250.200.1/32 \
      nexthop via 10.250.1.2 dev wg-bc-a weight 1 \
      nexthop via 10.250.2.2 dev wg-bc-b weight 1 \
      nexthop via 10.250.3.2 dev wg-bc-c weight 1
  "

  remote_c "
    sudo ip link add wg-bc-a type wireguard
    sudo ip addr add 10.250.1.2/30 dev wg-bc-a
    sudo wg set wg-bc-a private-key /tmp/wg-speed-bc-a.key listen-port 58101 peer '${b_bc_a}' allowed-ips 0.0.0.0/0 endpoint 10.91.1.12:58001 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-bc-a
    sudo ip link add wg-bc-b type wireguard
    sudo ip addr add 10.250.2.2/30 dev wg-bc-b
    sudo wg set wg-bc-b private-key /tmp/wg-speed-bc-b.key listen-port 58102 peer '${b_bc_b}' allowed-ips 0.0.0.0/0 endpoint 10.91.2.12:58002 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-bc-b
    sudo ip link add wg-bc-c type wireguard
    sudo ip addr add 10.250.3.2/30 dev wg-bc-c
    sudo wg set wg-bc-c private-key /tmp/wg-speed-bc-c.key listen-port 58103 peer '${b_bc_c}' allowed-ips 0.0.0.0/0 endpoint 10.91.3.12:58003 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-bc-c
    sudo ip link add wg-ca-a type wireguard
    sudo ip addr add 10.250.11.1/30 dev wg-ca-a
    sudo wg set wg-ca-a private-key /tmp/wg-speed-ca-a.key listen-port 58201 peer '${a_ca_a}' allowed-ips 0.0.0.0/0 endpoint 10.91.1.11:58301 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-ca-a
    sudo ip link add wg-ca-b type wireguard
    sudo ip addr add 10.250.12.1/30 dev wg-ca-b
    sudo wg set wg-ca-b private-key /tmp/wg-speed-ca-b.key listen-port 58202 peer '${a_ca_b}' allowed-ips 0.0.0.0/0 endpoint 10.91.2.11:58302 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-ca-b
    sudo ip link add wg-ca-c type wireguard
    sudo ip addr add 10.250.13.1/30 dev wg-ca-c
    sudo wg set wg-ca-c private-key /tmp/wg-speed-ca-c.key listen-port 58203 peer '${a_ca_c}' allowed-ips 0.0.0.0/0 endpoint 10.91.3.11:58303 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-ca-c
    sudo ip route replace 10.250.100.1/32 \
      nexthop via 10.250.1.1 dev wg-bc-a weight 1 \
      nexthop via 10.250.2.1 dev wg-bc-b weight 1 \
      nexthop via 10.250.3.1 dev wg-bc-c weight 1
    sudo ip route replace 10.250.200.1/32 \
      nexthop via 10.250.11.2 dev wg-ca-a weight 1 \
      nexthop via 10.250.12.2 dev wg-ca-b weight 1 \
      nexthop via 10.250.13.2 dev wg-ca-c weight 1
  "

  remote_a "
    sudo ip addr add 10.250.200.1/32 dev lo 2>/dev/null || true
    sudo ip link add wg-ca-a type wireguard
    sudo ip addr add 10.250.11.2/30 dev wg-ca-a
    sudo wg set wg-ca-a private-key /tmp/wg-speed-ca-a.key listen-port 58301 peer '${c_ca_a}' allowed-ips 0.0.0.0/0 endpoint 10.91.1.13:58201 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-ca-a
    sudo ip link add wg-ca-b type wireguard
    sudo ip addr add 10.250.12.2/30 dev wg-ca-b
    sudo wg set wg-ca-b private-key /tmp/wg-speed-ca-b.key listen-port 58302 peer '${c_ca_b}' allowed-ips 0.0.0.0/0 endpoint 10.91.2.13:58202 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-ca-b
    sudo ip link add wg-ca-c type wireguard
    sudo ip addr add 10.250.13.2/30 dev wg-ca-c
    sudo wg set wg-ca-c private-key /tmp/wg-speed-ca-c.key listen-port 58303 peer '${c_ca_c}' allowed-ips 0.0.0.0/0 endpoint 10.91.3.13:58203 persistent-keepalive 5
    sudo ip link set mtu 1280 up dev wg-ca-c
    sudo ip route replace 10.250.100.1/32 \
      nexthop via 10.250.11.1 dev wg-ca-a weight 1 \
      nexthop via 10.250.12.1 dev wg-ca-b weight 1 \
      nexthop via 10.250.13.1 dev wg-ca-c weight 1
  "
}

run_iperf() {
  local label="$1"
  local server_port="$2"
  local client_port="$3"
  local server_bind="$4"
  local client_target="$5"
  local client_bind="$6"
  local port_number="$7"
  local parallel="$8"
  local seconds="$9"

  record "## ${label}"
  remote "${server_port}" "pkill -x iperf3 2>/dev/null || true; iperf3 -s -D -1 -B ${server_bind} -p ${port_number} --logfile /tmp/${label}.gatherlink-routing-speed.server.log"
  sleep 1
  if ! remote "${client_port}" "iperf3 -c ${client_target} -B ${client_bind} -p ${port_number} -P ${parallel} -t ${seconds} -J" >"${OUT_DIR}/${label}.json" 2>"${OUT_DIR}/${label}.client.err"; then
    record "iperf client failed for ${label}"
    record "client stderr:"
    sed -n '1,120p' "${OUT_DIR}/${label}.client.err" | tee -a "${REPORT}"
    remote "${server_port}" "cat /tmp/${label}.gatherlink-routing-speed.server.log 2>/dev/null || true" >"${OUT_DIR}/${label}.server.log" || true
    record "server log:"
    sed -n '1,120p' "${OUT_DIR}/${label}.server.log" | tee -a "${REPORT}"
    return 1
  fi
  remote "${server_port}" "cat /tmp/${label}.gatherlink-routing-speed.server.log 2>/dev/null || true" >"${OUT_DIR}/${label}.server.log" || true
  python3 - "${OUT_DIR}/${label}.json" <<'PY' | tee -a "${REPORT}"
from __future__ import annotations

import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
end = data.get("end", {})
sent = end.get("sum_sent") or end.get("sum") or {}
received = end.get("sum_received") or {}
bps = received.get("bits_per_second") or sent.get("bits_per_second") or 0
retransmits = sent.get("retransmits", "-")
print(f"{path.stem}: {bps / 1_000_000:.2f} Mbit/s retrans={retransmits}")
PY
}

run_simultaneous_iperfs() {
  local label="$1"
  local seconds="$2"

  record "## ${label}"
  record "Starting six concurrent direct WireGuard iperf clients: three B-C paths and three C-A paths."

  remote_c "pkill -x iperf3 2>/dev/null || true; iperf3 -s -D -1 -B 10.250.1.2 -p 7111 --logfile /tmp/${label}-bc-a.server.log"
  remote_c "iperf3 -s -D -1 -B 10.250.2.2 -p 7112 --logfile /tmp/${label}-bc-b.server.log"
  remote_c "iperf3 -s -D -1 -B 10.250.3.2 -p 7113 --logfile /tmp/${label}-bc-c.server.log"
  remote_a "pkill -x iperf3 2>/dev/null || true; iperf3 -s -D -1 -B 10.250.11.2 -p 7121 --logfile /tmp/${label}-ca-a.server.log"
  remote_a "iperf3 -s -D -1 -B 10.250.12.2 -p 7122 --logfile /tmp/${label}-ca-b.server.log"
  remote_a "iperf3 -s -D -1 -B 10.250.13.2 -p 7123 --logfile /tmp/${label}-ca-c.server.log"
  sleep 1

  remote_b "iperf3 -c 10.250.1.2 -B 10.250.1.1 -p 7111 -P 2 -t ${seconds} -J" >"${OUT_DIR}/${label}-bc-a.json" 2>"${OUT_DIR}/${label}-bc-a.client.err" &
  local pid_bc_a=$!
  remote_b "iperf3 -c 10.250.2.2 -B 10.250.2.1 -p 7112 -P 2 -t ${seconds} -J" >"${OUT_DIR}/${label}-bc-b.json" 2>"${OUT_DIR}/${label}-bc-b.client.err" &
  local pid_bc_b=$!
  remote_b "iperf3 -c 10.250.3.2 -B 10.250.3.1 -p 7113 -P 2 -t ${seconds} -J" >"${OUT_DIR}/${label}-bc-c.json" 2>"${OUT_DIR}/${label}-bc-c.client.err" &
  local pid_bc_c=$!
  remote_c "iperf3 -c 10.250.11.2 -B 10.250.11.1 -p 7121 -P 2 -t ${seconds} -J" >"${OUT_DIR}/${label}-ca-a.json" 2>"${OUT_DIR}/${label}-ca-a.client.err" &
  local pid_ca_a=$!
  remote_c "iperf3 -c 10.250.12.2 -B 10.250.12.1 -p 7122 -P 2 -t ${seconds} -J" >"${OUT_DIR}/${label}-ca-b.json" 2>"${OUT_DIR}/${label}-ca-b.client.err" &
  local pid_ca_b=$!
  remote_c "iperf3 -c 10.250.13.2 -B 10.250.13.1 -p 7123 -P 2 -t ${seconds} -J" >"${OUT_DIR}/${label}-ca-c.json" 2>"${OUT_DIR}/${label}-ca-c.client.err" &
  local pid_ca_c=$!

  local failed=0
  for pid in "${pid_bc_a}" "${pid_bc_b}" "${pid_bc_c}" "${pid_ca_a}" "${pid_ca_b}" "${pid_ca_c}"; do
    if ! wait "${pid}"; then
      failed=1
    fi
  done

  remote_c "cat /tmp/${label}-bc-*.server.log 2>/dev/null || true" >"${OUT_DIR}/${label}-bc.server.log" || true
  remote_a "cat /tmp/${label}-ca-*.server.log 2>/dev/null || true" >"${OUT_DIR}/${label}-ca.server.log" || true

  if [[ "${failed}" -ne 0 ]]; then
    record "one or more simultaneous iperf clients failed"
    for err in "${OUT_DIR}/${label}"-*.client.err; do
      if [[ -s "${err}" ]]; then
        record "$(basename "${err}"):"
        sed -n '1,80p' "${err}" | tee -a "${REPORT}"
      fi
    done
    return 1
  fi

  python3 - "${OUT_DIR}" "${label}" <<'PY' | tee -a "${REPORT}"
from __future__ import annotations

import json
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
label = sys.argv[2]
groups = {
    "B-C simultaneous total": ["bc-a", "bc-b", "bc-c"],
    "C-A simultaneous total": ["ca-a", "ca-b", "ca-c"],
    "All six simultaneous total": ["bc-a", "bc-b", "bc-c", "ca-a", "ca-b", "ca-c"],
}
results: dict[str, tuple[float, int]] = {}
for suffix in groups["All six simultaneous total"]:
    path = out_dir / f"{label}-{suffix}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    end = data.get("end", {})
    sent = end.get("sum_sent") or end.get("sum") or {}
    received = end.get("sum_received") or {}
    bps = received.get("bits_per_second") or sent.get("bits_per_second") or 0
    retransmits = sent.get("retransmits") or 0
    results[suffix] = (float(bps), int(retransmits))
    print(f"{label}-{suffix}: {bps / 1_000_000:.2f} Mbit/s retrans={retransmits}")
for group, suffixes in groups.items():
    bps = sum(results[suffix][0] for suffix in suffixes)
    retransmits = sum(results[suffix][1] for suffix in suffixes)
    print(f"{group}: {bps / 1_000_000:.2f} Mbit/s retrans={retransmits}")
PY
}

record "# Direct WireGuard B-C-A Routing Baseline"
record ""
record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
record "- duration_seconds: ${DURATION}"
record "- end_to_end_parallel_streams: ${PARALLEL}"
record "- out: ${OUT_DIR}"
record ""

setup_wireguard
sleep 2

record "## Reachability"
remote_b "ping -c 2 -W 1 10.250.1.2" | tee -a "${REPORT}"
remote_c "ping -c 2 -W 1 10.250.11.2" | tee -a "${REPORT}"
remote_a "ping -c 2 -W 1 10.250.11.1" | tee -a "${REPORT}"
remote_b "ping -I 10.250.100.1 -c 2 -W 1 10.250.200.1" | tee -a "${REPORT}"
remote_a "ping -I 10.250.200.1 -c 2 -W 1 10.250.100.1" | tee -a "${REPORT}"

run_iperf "bc_path_a" "${PORT_C}" "${PORT_B}" "10.250.1.2" "10.250.1.2" "10.250.1.1" 7011 2 "${DURATION}"
run_iperf "bc_path_b" "${PORT_C}" "${PORT_B}" "10.250.2.2" "10.250.2.2" "10.250.2.1" 7012 2 "${DURATION}"
run_iperf "bc_path_c" "${PORT_C}" "${PORT_B}" "10.250.3.2" "10.250.3.2" "10.250.3.1" 7013 2 "${DURATION}"
run_iperf "ca_path_a" "${PORT_A}" "${PORT_C}" "10.250.11.2" "10.250.11.2" "10.250.11.1" 7021 2 "${DURATION}"
run_iperf "ca_path_b" "${PORT_A}" "${PORT_C}" "10.250.12.2" "10.250.12.2" "10.250.12.1" 7022 2 "${DURATION}"
run_iperf "ca_path_c" "${PORT_A}" "${PORT_C}" "10.250.13.2" "10.250.13.2" "10.250.13.1" 7023 2 "${DURATION}"
run_simultaneous_iperfs "simultaneous_direct_wireguard_paths" "${DURATION}"
run_iperf "e2e_b_to_a_via_c" "${PORT_A}" "${PORT_B}" "10.250.200.1" "10.250.200.1" "10.250.100.1" 7030 "${PARALLEL}" "${DURATION}"

record ""
record "Report: ${REPORT}"
if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
  record "Temporary WireGuard interfaces were left up for inspection."
fi
printf '\nDirect WireGuard routing speed complete.\nReport: %s\n' "${REPORT}"
