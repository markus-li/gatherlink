#!/usr/bin/env python3
"""
Write deterministic Hyper-V VM management netplan into a mounted Debian root.

This helper is intentionally offline-only. Use it after mounting a stopped VM's
cloud-image root filesystem when Hyper-V Default Switch DHCP or stale ARP makes
the normal management address unreliable.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

HOSTS = {
    "11": ("00:15:5d:91:00:01", "172.26.209.11"),
    "12": ("00:15:5d:92:00:01", "172.26.209.12"),
    "13": ("00:15:5d:93:00:01", "172.26.209.13"),
}


def parse_args() -> argparse.Namespace:
    """Parse the offline VM-root update arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Mounted VM root filesystem.")
    parser.add_argument("--host-index", required=True, choices=sorted(HOSTS), help="Gatherlink VM host index.")
    parser.add_argument("--gateway", default="172.26.208.1", help="Default Switch host gateway.")
    parser.add_argument("--prefix", default="20", help="CIDR prefix for the static management address.")
    return parser.parse_args()


def main() -> int:
    """Rewrite the mounted VM root with deterministic management networking."""
    args = parse_args()
    mac, address = HOSTS[args.host_index]
    netplan = args.root / "etc/netplan/50-cloud-init.yaml"
    cloud_cfg = args.root / "etc/cloud/cloud.cfg.d/99-gatherlink-disable-network-regeneration.cfg"
    if not netplan.exists():
        raise SystemExit(f"netplan file not found: {netplan}")

    backup = netplan.with_suffix(netplan.suffix + ".gatherlink-bak")
    if not backup.exists():
        shutil.copy2(netplan, backup)

    lines = netplan.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    index = 0
    replaced = False
    while index < len(lines):
        line = lines[index]
        if line == "    internet:":
            output.extend(
                [
                    "    internet:",
                    "      match:",
                    f'        macaddress: "{mac}"',
                    "      addresses:",
                    f'      - "{address}/{args.prefix}"',
                    "      routes:",
                    "      - to: default",
                    f'        via: "{args.gateway}"',
                    "      nameservers:",
                    "        addresses:",
                    '        - "1.1.1.1"',
                    '        - "8.8.8.8"',
                    "      dhcp4: false",
                    "      dhcp6: false",
                    '      set-name: "internet"',
                ]
            )
            index += 1
            while index < len(lines):
                next_line = lines[index]
                if next_line.startswith("    ") and not next_line.startswith("      "):
                    break
                index += 1
            replaced = True
            continue
        output.append(line)
        index += 1

    if not replaced:
        raise SystemExit(f"internet stanza not found in {netplan}")

    netplan.write_text("\n".join(output) + "\n", encoding="utf-8")
    cloud_cfg.write_text("network: {config: disabled}\n", encoding="utf-8")
    print(f"configured internet {address}/{args.prefix} via {args.gateway} in {netplan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
