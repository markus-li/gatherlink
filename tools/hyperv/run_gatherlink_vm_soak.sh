#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DURATION=3600
SHAPE_PROFILE="asymmetric"
MIN_DELIVERY_RATIO="0.90"
OUT_DIR="${REPO_ROOT}/.gatherlink/hyperv-vm-soak/$(date -u +%Y%m%dT%H%M%SZ)"
EXTRA_ARGS=()

usage() {
  cat <<'USAGE'
Usage: run_gatherlink_vm_soak.sh --host-key-a KEY --host-key-b KEY [options]

Prepares the v0.9 Hyper-V two-VM soak command. The default is a one-hour run
through the normal Hyper-V VM acceptance runner, using managed services, real
per-path UDP sockets, diagnostics capture, and cleanup.

Options:
  --duration SECONDS        Soak duration. Default 3600.
  --shape-profile NAME      clean, asymmetric, lossy, latency, or none. Default asymmetric.
  --min-delivery-ratio N    Minimum receive/send ratio. Default 0.90.
  --out DIR                 Report directory.

All other options are passed through to run_gatherlink_vm_acceptance.sh, such as
--inventory, --ip-a, --ip-b, --host-key-a, --host-key-b, --branch, and
--skip-build.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration|--soak)
      DURATION="$2"
      shift 2
      ;;
    --shape-profile)
      SHAPE_PROFILE="$2"
      shift 2
      ;;
    --min-delivery-ratio)
      MIN_DELIVERY_RATIO="$2"
      shift 2
      ;;
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

exec "${SCRIPT_DIR}/run_gatherlink_vm_acceptance.sh" \
  --count 5 \
  --duration "${DURATION}" \
  --payload-size 512 \
  --interval 0.002 \
  --shape-profile "${SHAPE_PROFILE}" \
  --min-delivery-ratio "${MIN_DELIVERY_RATIO}" \
  --out "${OUT_DIR}" \
  "${EXTRA_ARGS[@]}"
