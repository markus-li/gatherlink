#!/usr/bin/env bash
# Configure Gatherlink Hyper-V private path interfaces inside a Debian guest.
#
# The cloud-image seed config names path-a/path-b/path-c on first boot. When a
# running lab is extended with path-d/path-e, this helper gives the new NICs the
# same deterministic names and addresses without depending on Hyper-V commands
# inside the guest.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: configure_guest_path_interfaces.sh --host-index 11|12|13 [--paths a,b,c,d,e] [--mtu BYTES]

Examples:
  sudo tools/hyperv/configure_guest_path_interfaces.sh --host-index 11 --paths d,e
  sudo tools/hyperv/configure_guest_path_interfaces.sh --host-index 12 --paths a,b,c,d,e
USAGE
}

HOST_INDEX=""
PATHS="a,b,c,d,e"
MTU=1500

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host-index) HOST_INDEX="$2"; shift 2 ;;
    --paths) PATHS="$2"; shift 2 ;;
    --mtu) MTU="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "${HOST_INDEX}" in
  11|12|13) ;;
  *) echo "--host-index must be 11, 12, or 13" >&2; exit 2 ;;
esac

mac_base_for_host() {
  case "$1" in
    11) printf '00:15:5d:91:00' ;;
    12) printf '00:15:5d:92:00' ;;
    13) printf '00:15:5d:93:00' ;;
  esac
}

path_number() {
  case "$1" in
    a) printf '1' ;;
    b) printf '2' ;;
    c) printf '3' ;;
    d) printf '4' ;;
    e) printf '5' ;;
    *) echo "unsupported path '$1'; expected a,b,c,d,e" >&2; exit 2 ;;
  esac
}

find_device_by_mac() {
  local expected_mac="$1"
  local device
  for path in /sys/class/net/*; do
    [[ -e "${path}/address" ]] || continue
    if [[ "$(cat "${path}/address")" == "${expected_mac}" ]]; then
      device="$(basename "${path}")"
      printf '%s\n' "${device}"
      return 0
    fi
  done
  return 1
}

mac_base="$(mac_base_for_host "${HOST_INDEX}")"
IFS=',' read -r -a path_letters <<<"${PATHS}"

for letter in "${path_letters[@]}"; do
  letter="${letter//[[:space:]]/}"
  [[ -n "${letter}" ]] || continue

  number="$(path_number "${letter}")"
  target_name="path-${letter}"
  target_mac="${mac_base}:$(printf '%s1' "${letter}")"
  target_ip="10.91.${number}.${HOST_INDEX}/24"

  device="$(find_device_by_mac "${target_mac}")" || {
    echo "missing ${target_name}: no interface with MAC ${target_mac}" >&2
    exit 1
  }

  if [[ "${device}" != "${target_name}" ]]; then
    ip link set dev "${device}" down
    ip link set dev "${device}" name "${target_name}"
  fi

  ip addr flush dev "${target_name}"
  ip addr add "${target_ip}" dev "${target_name}"
  ip link set dev "${target_name}" mtu "${MTU}" up
  echo "configured ${target_name} ${target_ip} mtu=${MTU}"
done
