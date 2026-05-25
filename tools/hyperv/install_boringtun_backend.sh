#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/perf_common.sh"

IP="172.22.0.1"
PORTS="2201,2202,2203"
BORINGTUN_VERSION="${BORINGTUN_VERSION:-0.5.2}"
BORINGTUN_RUST_TOOLCHAIN="${BORINGTUN_RUST_TOOLCHAIN:-1.88.0}"

usage() {
  cat <<'USAGE'
Usage: install_boringtun_backend.sh [options]

Installs the optional Cloudflare BoringTun benchmark backend on the Hyper-V
Debian guests. This is benchmark tooling only; it does not make BoringTun a
Gatherlink runtime dependency or helper default.

Options:
  --ip IP             Management IP used with WSL portproxy. Default 172.22.0.1.
  --ports LIST        Comma-separated SSH ports. Default 2201,2202,2203.
  --version VERSION   boringtun-cli crate version. Default $BORINGTUN_VERSION or 0.5.2.
  --rust-toolchain V  Rust toolchain to install with rustup. Default 1.88.0.
  --out DIR           Report directory.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --ports) PORTS="$2"; shift 2 ;;
    --version) BORINGTUN_VERSION="$2"; shift 2 ;;
    --rust-toolchain) BORINGTUN_RUST_TOOLCHAIN="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

perf_init_defaults
REPORT="${OUT_DIR}/boringtun-install-report.md"
: >"${REPORT}"

perf_record "# BoringTun Backend Install"
perf_record ""
perf_record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
perf_record "- boringtun_cli_version: ${BORINGTUN_VERSION}"
perf_record "- rust_toolchain: ${BORINGTUN_RUST_TOOLCHAIN}"
perf_record "- ports: ${PORTS}"
perf_record ""

install_on_port() {
  local port="$1"
  perf_step "VM port ${port}"
  perf_remote "${port}" "
    set -euo pipefail
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends ca-certificates curl pkg-config
    if ! command -v rustup >/dev/null 2>&1; then
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs |
        sh -s -- -y --profile minimal --default-toolchain '${BORINGTUN_RUST_TOOLCHAIN}'
    fi
    ~/.cargo/bin/rustup toolchain install '${BORINGTUN_RUST_TOOLCHAIN}' --profile minimal
    ~/.cargo/bin/cargo +${BORINGTUN_RUST_TOOLCHAIN} install boringtun-cli \
      --version '${BORINGTUN_VERSION}' \
      --force
    sudo install -m 0755 ~/.cargo/bin/boringtun-cli /usr/local/bin/boringtun-cli
    sudo setcap cap_net_admin+epi /usr/local/bin/boringtun-cli || true
    command -v boringtun-cli
    boringtun-cli --version || true
  " | tee -a "${REPORT}"
}

IFS=',' read -r -a ports <<<"${PORTS}"
for port in "${ports[@]}"; do
  port="$(printf '%s' "${port}" | xargs)"
  [[ -n "${port}" ]] || continue
  install_on_port "${port}"
done

perf_record ""
perf_record "Install complete. Run:"
perf_record ""
perf_record '```bash'
perf_record "tools/hyperv/run_wireguard_onehop_speed.sh --implementation boringtun --active-paths a,b,c"
perf_record '```'
