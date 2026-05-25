#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/perf_common.sh"

IP="172.22.0.1"
PORTS="2201,2202,2203"
GOTATUN_REPO="https://github.com/mullvad/gotatun.git"
GOTATUN_REF="${GOTATUN_REF:-v0.7.0}"
GOTATUN_RUST_TOOLCHAIN="${GOTATUN_RUST_TOOLCHAIN:-1.88.0}"
INSTALL_DIR="/opt/gotatun-src"

usage() {
  cat <<'USAGE'
Usage: install_gotatun_backend.sh [options]

Installs the optional GotaTun benchmark backend on the Hyper-V Debian guests.
This is benchmark tooling only; it does not make GotaTun a Gatherlink runtime
dependency or helper default.

Options:
  --ip IP             Management IP used with WSL portproxy. Default 172.22.0.1.
  --ports LIST        Comma-separated SSH ports. Default 2201,2202,2203.
  --ref REF           Git tag/commit/branch to build. Default $GOTATUN_REF or v0.7.0.
  --repo URL          GotaTun repository URL. Default https://github.com/mullvad/gotatun.git.
  --rust-toolchain V  Rust toolchain to install with rustup. Default 1.88.0.
  --install-dir PATH  Source checkout path on each VM. Default /opt/gotatun-src.
  --out DIR           Report directory.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --ports) PORTS="$2"; shift 2 ;;
    --ref) GOTATUN_REF="$2"; shift 2 ;;
    --repo) GOTATUN_REPO="$2"; shift 2 ;;
    --rust-toolchain) GOTATUN_RUST_TOOLCHAIN="$2"; shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

perf_init_defaults
case "${INSTALL_DIR}" in
  /|""|".")
    echo "--install-dir must be an absolute, non-root path" >&2
    exit 2
    ;;
  /*) ;;
  *)
    echo "--install-dir must be absolute" >&2
    exit 2
    ;;
esac
REPORT="${OUT_DIR}/gotatun-install-report.md"
: >"${REPORT}"

perf_record "# GotaTun Backend Install"
perf_record ""
perf_record "- generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
perf_record "- repo: ${GOTATUN_REPO}"
perf_record "- ref: ${GOTATUN_REF}"
perf_record "- rust_toolchain: ${GOTATUN_RUST_TOOLCHAIN}"
perf_record "- ports: ${PORTS}"
perf_record ""

install_on_port() {
  local port="$1"
  perf_step "VM port ${port}"
  perf_remote "${port}" "
    set -euo pipefail
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends git ca-certificates curl pkg-config libclang-dev
    if ! command -v rustup >/dev/null 2>&1; then
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs |
        sh -s -- -y --profile minimal --default-toolchain '${GOTATUN_RUST_TOOLCHAIN}'
    fi
    ~/.cargo/bin/rustup toolchain install '${GOTATUN_RUST_TOOLCHAIN}' --profile minimal
    sudo rm -rf '${INSTALL_DIR}'
    sudo git clone --depth 1 --branch '${GOTATUN_REF}' '${GOTATUN_REPO}' '${INSTALL_DIR}'
    sudo chown -R gatherlink:gatherlink '${INSTALL_DIR}'
    cd '${INSTALL_DIR}'
    ~/.cargo/bin/cargo +${GOTATUN_RUST_TOOLCHAIN} build --bin gotatun --release
    sudo install -m 0755 target/release/gotatun /usr/local/bin/gotatun
    sudo setcap cap_net_admin+epi /usr/local/bin/gotatun || true
    command -v gotatun
    gotatun --help >/tmp/gotatun-help.txt 2>&1 || true
    printf 'installed_ref=%s\n' '${GOTATUN_REF}'
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
perf_record "tools/hyperv/run_wireguard_onehop_speed.sh --implementation gotatun --active-paths a,b,c"
perf_record '```'
