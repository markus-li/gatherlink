#!/usr/bin/env bash
set -euo pipefail

DEVICE="${DEVICE:-lo}"
ROOT_HANDLE="${ROOT_HANDLE:-88:}"
DEFAULT_CLASS="${DEFAULT_CLASS:-999}"

declare -A RATE=(
  ["path-a.ab"]="${PATH_A_AB_RATE:-50mbit}"
  ["path-a.ba"]="${PATH_A_BA_RATE:-50mbit}"
  ["path-b.ab"]="${PATH_B_AB_RATE:-50mbit}"
  ["path-b.ba"]="${PATH_B_BA_RATE:-50mbit}"
  ["path-c.ab"]="${PATH_C_AB_RATE:-50mbit}"
  ["path-c.ba"]="${PATH_C_BA_RATE:-50mbit}"
)

declare -A DST_IP=(
  ["path-a.ab"]="10.88.1.12"
  ["path-a.ba"]="10.88.1.11"
  ["path-b.ab"]="10.88.2.12"
  ["path-b.ba"]="10.88.2.11"
  ["path-c.ab"]="10.88.3.12"
  ["path-c.ba"]="10.88.3.11"
)

declare -A DST_PORT=(
  ["path-a.ab"]="57001"
  ["path-a.ba"]="56001"
  ["path-b.ab"]="57002"
  ["path-b.ba"]="56002"
  ["path-c.ab"]="57003"
  ["path-c.ba"]="56003"
)

declare -A CLASS_ID=(
  ["path-a.ab"]="11"
  ["path-a.ba"]="12"
  ["path-b.ab"]="21"
  ["path-b.ba"]="22"
  ["path-c.ab"]="31"
  ["path-c.ba"]="32"
)

usage() {
  cat <<'EOF'
Usage:
  sudo tools/wsl_shape_private_lan.sh apply [path-a.ab=3mbit path-a.ba=3mbit ...]
  sudo tools/wsl_shape_private_lan.sh clear
  sudo tools/wsl_shape_private_lan.sh show

Directions:
  path-a.ab  node A -> node B carrier traffic, 10.88.1.11:56001 -> 10.88.1.12:57001
  path-a.ba  node B -> node A carrier traffic, 10.88.1.12:57001 -> 10.88.1.11:56001
  path-b.ab  node A -> node B carrier traffic, 10.88.2.11:56002 -> 10.88.2.12:57002
  path-b.ba  node B -> node A carrier traffic, 10.88.2.12:57002 -> 10.88.2.11:56002
  path-c.ab  node A -> node B carrier traffic, 10.88.3.11:56003 -> 10.88.3.12:57003
  path-c.ba  node B -> node A carrier traffic, 10.88.3.12:57003 -> 10.88.3.11:56003

Examples:
  sudo tools/wsl_shape_private_lan.sh apply path-a.ab=3mbit path-a.ba=2mbit path-b.ab=1mbit path-b.ba=1500kbit path-c.ab=750kbit path-c.ba=750kbit
  sudo tools/wsl_shape_private_lan.sh clear
EOF
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "run with sudo so tc can change qdisc state on ${DEVICE}" >&2
    exit 1
  fi
}

parse_rates() {
  local assignment key value
  for assignment in "$@"; do
    key="${assignment%%=*}"
    value="${assignment#*=}"
    if [[ "${key}" == "${assignment}" || -z "${value}" || -z "${RATE[$key]+set}" ]]; then
      echo "invalid rate assignment: ${assignment}" >&2
      usage >&2
      exit 1
    fi
    RATE["${key}"]="${value}"
  done
}

apply_shape() {
  require_root
  parse_rates "$@"

  tc qdisc replace dev "${DEVICE}" root handle "${ROOT_HANDLE}" htb default "${DEFAULT_CLASS}"
  tc class replace dev "${DEVICE}" parent "${ROOT_HANDLE}" classid 88:${DEFAULT_CLASS} htb rate 10gbit ceil 10gbit

  local key class_id
  for key in path-a.ab path-a.ba path-b.ab path-b.ba path-c.ab path-c.ba; do
    class_id="${CLASS_ID[$key]}"
    tc class replace dev "${DEVICE}" parent "${ROOT_HANDLE}" classid "88:${class_id}" htb rate "${RATE[$key]}" ceil "${RATE[$key]}"
    tc filter replace dev "${DEVICE}" protocol ip parent "${ROOT_HANDLE}" prio "${class_id}" flower \
      ip_proto udp dst_ip "${DST_IP[$key]}" dst_port "${DST_PORT[$key]}" classid "88:${class_id}"
  done

  show_shape
}

clear_shape() {
  require_root
  tc qdisc del dev "${DEVICE}" root 2>/dev/null || true
  echo "cleared WSL private LAN shaping on ${DEVICE}"
}

show_shape() {
  echo "qdisc:"
  tc qdisc show dev "${DEVICE}"
  echo
  echo "classes:"
  tc class show dev "${DEVICE}"
  echo
  echo "filters:"
  tc filter show dev "${DEVICE}" parent "${ROOT_HANDLE}" || true
}

command="${1:-}"
case "${command}" in
  apply)
    shift
    apply_shape "$@"
    ;;
  clear)
    clear_shape
    ;;
  show)
    show_shape
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "unknown command: ${command}" >&2
    usage >&2
    exit 1
    ;;
esac
