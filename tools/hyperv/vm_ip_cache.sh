#!/usr/bin/env bash

# Shared Hyper-V VM IP cache helpers for WSL/Bash runners.
#
# Hyper-V Default Switch DHCP addresses are usually stable during a lab session,
# but resolving them through PowerShell/ARP is noisy and occasionally needs
# elevated host access. Keep discovered addresses in the ignored project state
# directory and only rediscover when no cached address is available.

hyperv_vm_ip_cache_file() {
  local repo_root="$1"
  printf '%s\n' "${HYPERV_VM_IP_CACHE:-${repo_root}/.gatherlink/hyperv-vm-ip-cache.env}"
}

hyperv_vm_ip_cache_key() {
  local name="$1"
  printf 'HYPERV_VM_IP_%s\n' "$(printf '%s' "${name}" | tr '[:lower:]-.' '[:upper:]__')"
}

hyperv_cached_vm_ip() {
  local repo_root="$1"
  local name="$2"
  local cache_file key
  cache_file="$(hyperv_vm_ip_cache_file "${repo_root}")"
  key="$(hyperv_vm_ip_cache_key "${name}")"
  if [[ -f "${cache_file}" ]]; then
    # shellcheck disable=SC1090
    source "${cache_file}"
  fi
  printf '%s\n' "${!key:-}"
}

hyperv_cache_vm_ip() {
  local repo_root="$1"
  local name="$2"
  local ip="$3"
  local cache_file key tmp_file
  [[ -n "${ip}" ]] || return 0
  cache_file="$(hyperv_vm_ip_cache_file "${repo_root}")"
  key="$(hyperv_vm_ip_cache_key "${name}")"
  mkdir -p "$(dirname "${cache_file}")"
  tmp_file="$(mktemp "${cache_file}.XXXXXX")"
  if [[ -f "${cache_file}" ]]; then
    grep -v "^${key}=" "${cache_file}" >"${tmp_file}" || true
  else
    : >"${tmp_file}"
  fi
  printf '%s=%q\n' "${key}" "${ip}" >>"${tmp_file}"
  mv "${tmp_file}" "${cache_file}"
}

hyperv_resolve_vm_ip() {
  local repo_root="$1"
  local script_dir="$2"
  local name="$3"
  local explicit_ip="${4:-}"
  local cached helper_windows resolved
  if [[ -n "${explicit_ip}" ]]; then
    hyperv_cache_vm_ip "${repo_root}" "${name}" "${explicit_ip}"
    printf '%s\n' "${explicit_ip}"
    return 0
  fi
  cached="$(hyperv_cached_vm_ip "${repo_root}" "${name}")"
  if [[ -n "${cached}" ]]; then
    printf '%s\n' "${cached}"
    return 0
  fi
  helper_windows="$(wslpath -w "${script_dir}/resolve_gatherlink_vm.ps1")"
  resolved="$(powershell.exe -ExecutionPolicy Bypass -File "${helper_windows}" -Name "${name}" | tr -d '\r')"
  hyperv_cache_vm_ip "${repo_root}" "${name}" "${resolved}"
  printf '%s\n' "${resolved}"
}
