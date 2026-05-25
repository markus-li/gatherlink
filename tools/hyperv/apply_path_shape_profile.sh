#!/usr/bin/env bash
set -euo pipefail

IP="172.22.0.1"
PORTS="2201,2202"
PERF_USER="${PERF_USER:-gatherlink}"
ACTIVE_PATHS="a,b,c"
PROFILE="clean"
LINK_MTU=1500

usage() {
  cat <<'USAGE'
Usage: apply_path_shape_profile.sh [options]

Applies repeatable Hyper-V path qdisc profiles to one or more Gatherlink VMs.
The profiles mirror docs/benchmarks/thresholds.json where practical so raw
Gatherlink and WireGuard-over-Gatherlink can be compared on the same simulated
WAN shapes.

Options:
  --ip IP                Management IP used with WSL portproxy. Default 172.22.0.1.
  --ports LIST           Comma-separated SSH ports. Default 2201,2202.
  --active-paths LIST    Comma-separated path letters. Default a,b,c; supports a,b,c,d,e.
  --profile NAME         clean, none, acceptance-300-500-700, acceptance-uneven-high,
                         realworld-fiber-plus-5g, realworld-starlink-plus-5g,
                         realworld-starlink-plus-2x5g, external-clean-dual-gig,
                         external-fiber-5g-asymmetric,
                         external-starlink-5g-high-bdp,
                         external-starlink-queue-dynamics,
                         external-five-starlink-correlated,
                         external-dual-lte-same-tower,
                         external-dual-lte-independent,
                         external-duplication-mode, or
                         external-tcp-mode-relay. Default clean.
  --link-mtu BYTES       MTU to set on active path interfaces. Default 1500.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --ports) PORTS="$2"; shift 2 ;;
    --active-paths) ACTIVE_PATHS="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --link-mtu) LINK_MTU="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

shape_command_for_path() {
  local path="$1"
  case "${PROFILE}:${path}" in
    clean:*|none:*)
      printf 'sudo tc qdisc del dev path-%s root 2>/dev/null || true; sudo ip link set path-%s mtu %s up' \
        "${path}" "${path}" "${LINK_MTU}"
      ;;
    acceptance-300-500-700:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 300mbit limit 131072' "${LINK_MTU}"
      ;;
    acceptance-300-500-700:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 500mbit limit 131072' "${LINK_MTU}"
      ;;
    acceptance-300-500-700:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 700mbit limit 131072' "${LINK_MTU}"
      ;;
    acceptance-uneven-high:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 600mbit limit 4096' "${LINK_MTU}"
      ;;
    acceptance-uneven-high:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 900mbit limit 4096' "${LINK_MTU}"
      ;;
    acceptance-uneven-high:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 1300mbit limit 4096' "${LINK_MTU}"
      ;;
    realworld-fiber-plus-5g:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 800mbit delay 12ms 3ms limit 4096' "${LINK_MTU}"
      ;;
    realworld-fiber-plus-5g:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 160mbit delay 45ms 15ms loss 0.2%% limit 2048' "${LINK_MTU}"
      ;;
    realworld-fiber-plus-5g:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 85mbit delay 70ms 25ms loss 0.5%% limit 2048' "${LINK_MTU}"
      ;;
    realworld-starlink-plus-5g:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 180mbit delay 45ms 25ms loss 0.3%% limit 2048' "${LINK_MTU}"
      ;;
    realworld-starlink-plus-5g:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 120mbit delay 55ms 20ms loss 0.6%% limit 2048' "${LINK_MTU}"
      ;;
    realworld-starlink-plus-5g:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 15mbit delay 95ms 35ms loss 1%% limit 512' "${LINK_MTU}"
      ;;
    realworld-starlink-plus-2x5g:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 180mbit delay 45ms 25ms loss 0.3%% limit 2048' "${LINK_MTU}"
      ;;
    realworld-starlink-plus-2x5g:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 140mbit delay 55ms 20ms loss 0.6%% limit 2048' "${LINK_MTU}"
      ;;
    realworld-starlink-plus-2x5g:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 90mbit delay 80ms 30ms loss 0.8%% limit 2048' "${LINK_MTU}"
      ;;
    external-clean-dual-gig:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 1000mbit delay 3ms 1ms limit 131072' "${LINK_MTU}"
      ;;
    external-clean-dual-gig:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 1000mbit delay 4ms 1ms limit 131072' "${LINK_MTU}"
      ;;
    external-fiber-5g-asymmetric:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 800mbit delay 5ms 1ms limit 8192' "${LINK_MTU}"
      ;;
    external-fiber-5g-asymmetric:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 150mbit delay 23ms 12ms loss 0.3%% limit 2048' "${LINK_MTU}"
      ;;
    external-starlink-5g-high-bdp:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 180mbit delay 28ms 25ms loss 0.3%% limit 4096' "${LINK_MTU}"
      ;;
    external-starlink-5g-high-bdp:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 120mbit delay 23ms 20ms loss 0.6%% limit 4096' "${LINK_MTU}"
      ;;
    external-starlink-5g-high-bdp:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 15mbit delay 45ms 30ms loss 1%% limit 1024' "${LINK_MTU}"
      ;;
    external-starlink-queue-dynamics:a)
      # Queue-dynamics deliberately keeps a shallow-ish queue on the fastest
      # satellite-like path. The goal is to expose latency-under-load and
      # scheduler recovery behavior, not to create a perfectly stable pipe.
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 160mbit delay 38ms 35ms loss 0.4%% limit 768' "${LINK_MTU}"
      ;;
    external-starlink-queue-dynamics:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 95mbit delay 52ms 45ms loss 0.8%% limit 512' "${LINK_MTU}"
      ;;
    external-starlink-queue-dynamics:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 20mbit delay 90ms 70ms loss 1.5%% limit 256' "${LINK_MTU}"
      ;;
    external-five-starlink-correlated:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 220mbit delay 45ms 20ms loss 0.3%% limit 4096' "${LINK_MTU}"
      ;;
    external-five-starlink-correlated:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 240mbit delay 55ms 25ms loss 0.4%% limit 4096' "${LINK_MTU}"
      ;;
    external-five-starlink-correlated:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 200mbit delay 65ms 35ms loss 0.6%% limit 4096' "${LINK_MTU}"
      ;;
    external-five-starlink-correlated:d)
      printf 'sudo ip link set path-d mtu %s up; sudo tc qdisc replace dev path-d root netem rate 230mbit delay 75ms 45ms loss 0.8%% limit 4096' "${LINK_MTU}"
      ;;
    external-five-starlink-correlated:e)
      printf 'sudo ip link set path-e mtu %s up; sudo tc qdisc replace dev path-e root netem rate 210mbit delay 85ms 55ms loss 1%% limit 4096' "${LINK_MTU}"
      ;;
    external-dual-lte-same-tower:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 80mbit delay 35ms 20ms loss 0.6%% limit 1024' "${LINK_MTU}"
      ;;
    external-dual-lte-same-tower:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 80mbit delay 42ms 22ms loss 0.6%% limit 1024' "${LINK_MTU}"
      ;;
    external-dual-lte-independent:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 80mbit delay 30ms 12ms loss 0.2%% limit 2048' "${LINK_MTU}"
      ;;
    external-dual-lte-independent:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 80mbit delay 55ms 18ms loss 0.3%% limit 2048' "${LINK_MTU}"
      ;;
    external-duplication-mode:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 120mbit delay 35ms 25ms loss 1%% limit 2048' "${LINK_MTU}"
      ;;
    external-duplication-mode:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 80mbit delay 55ms 30ms loss 2%% limit 2048' "${LINK_MTU}"
      ;;
    external-duplication-mode:c)
      printf 'sudo ip link set path-c mtu %s up; sudo tc qdisc replace dev path-c root netem rate 60mbit delay 85ms 45ms loss 3%% limit 1024' "${LINK_MTU}"
      ;;
    external-tcp-mode-relay:a)
      printf 'sudo ip link set path-a mtu %s up; sudo tc qdisc replace dev path-a root netem rate 200mbit delay 35ms 20ms loss 2%% limit 1024' "${LINK_MTU}"
      ;;
    external-tcp-mode-relay:b)
      printf 'sudo ip link set path-b mtu %s up; sudo tc qdisc replace dev path-b root netem rate 120mbit delay 55ms 10ms limit 2048' "${LINK_MTU}"
      ;;
    *)
      echo "unsupported profile/path: ${PROFILE}/${path}" >&2
      exit 2
      ;;
  esac
}

IFS=',' read -r -a ports <<<"${PORTS}"
IFS=',' read -r -a paths <<<"${ACTIVE_PATHS}"
for port in "${ports[@]}"; do
  [[ -n "${port}" ]] || continue
  for path in "${paths[@]}"; do
    path="${path//[[:space:]]/}"
    [[ -n "${path}" ]] || continue
    command="$(shape_command_for_path "${path}")"
    ssh -n -p "${port}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${PERF_USER}@${IP}" "${command}"
  done
done
