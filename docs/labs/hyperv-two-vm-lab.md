# Hyper-V Two-VM Gatherlink Lab

This document records the Gatherlink-specific Hyper-V lab shape. It does not
cover how to enable Hyper-V, install the Hyper-V PowerShell module, or prepare
Debian installation media. Use the official Microsoft Hyper-V documentation and
Debian installation documentation for those generic steps.

## VM Shape

Use two Debian VMs:

- `gatherlink-vm-a`
- `gatherlink-vm-b`

Agreed VM settings:

- Generation 2
- 2 vCPU
- 4 GB RAM
- dynamic memory disabled for repeatable network tests
- 48 GB dynamically expanding VHDX
- VM storage rooted under `D:\hyper-v\gatherlink\`
- checkpoints only when intentionally taking a manual restore point

The VHDX files are dynamic, so they do not reserve the full 48 GB on the host
drive up front.

## Hyper-V Switches

Each VM has four NICs:

- `External Network`: existing external/internet switch for package installs,
  SSH, Git, and normal management
- `gatherlink-path-a`: private switch for Gatherlink path A
- `gatherlink-path-b`: private switch for Gatherlink path B
- `gatherlink-path-c`: private switch for Gatherlink path C

Create or verify the Gatherlink private switches with:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\setup_gatherlink_switches.ps1
```

Run that from an elevated PowerShell prompt at the repository root. The script
only creates/reuses the three Gatherlink private switches and verifies that the
existing `External Network` switch is present.

## Debian Install Media

The manual installer path uses Debian amd64 netinst media. Download the current
Debian stable netinst ISO with:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\download_debian_netinst.ps1
```

The script stores the ISO under `D:\media\debian\`. It intentionally uses the
official Debian `current` netinst index instead of pinning a version in this
repo.

The repeatable lab path uses the official Debian generic cloud image plus a
NoCloud seed ISO. That seed creates the `gatherlink` user, injects an operator
provided SSH public key, and configures the three static path NICs. Keep the
public key in a host-local file outside Git, then run:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\prepare_gatherlink_cloud_vms.ps1 -PublicKeyPath C:\path\to\authorized_key.pub
```

Do not commit host-local public key files, generated seed files, or VM disks.

The `gatherlink` user is the normal lab login account. It has passwordless sudo
for lab setup and traffic shaping, but Gatherlink services should still run
unprivileged unless a specific lab setup command needs elevation.

When using Pageant-backed keys from Windows automation, use PuTTY `plink` with
agent forwarding enabled:

```powershell
plink.exe -agent -l gatherlink <vm-management-ip> "hostname; ip -br addr"
```

If Windows OpenSSH has a stale `SSH_AUTH_SOCK` pointing to a missing pipe, clear
the user-level override and open a new terminal:

```powershell
[Environment]::SetEnvironmentVariable("SSH_AUTH_SOCK", $null, "User")
```

The OpenSSH config may still contain Pageant's generated `IdentityAgent` line,
but `plink.exe -agent` is the verified Pageant path for this lab.

The VM management addresses come from Hyper-V's `Default Switch` DHCP and may
change after reboot. Resolve the current address by VM name with:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\resolve_gatherlink_vm.ps1 -Name gatherlink-vm-a
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\resolve_gatherlink_vm.ps1 -Name gatherlink-vm-b
```

Run a command through Pageant-backed `plink` with:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\invoke_gatherlink_vm.ps1 -Name gatherlink-vm-a -RemoteCommand "hostname; ip -br addr"
```

For unattended runs, pin the current PuTTY host-key fingerprint so the command
can stay non-interactive:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\invoke_gatherlink_vm.ps1 -Name gatherlink-vm-a -HostKey "<host-key-fingerprint>" -RemoteCommand "hostname"
```

## Source Sync

Use Git, not source archives, to move Gatherlink code into the VMs. Each VM owns
a bare repository at `/home/gatherlink/repos/gatherlink.git` and checks out its
working tree from that local bare repository into
`/home/gatherlink/src/gatherlink`.

The primary source-sync path is the WSL/Bash acceptance runner documented below.
For Windows-only maintenance, the PowerShell helper is:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\sync_gatherlink_vm_source.ps1 -HostKeyA "<vm-a-host-key>" -HostKeyB "<vm-b-host-key>" -Install
```

`-Install` refreshes the VM virtualenv and builds the Rust PyO3 dataplane with
`maturin develop`. Omit it for a fast source-only push after the VM is already
prepared.

From the WSL development checkout, push the current branch to each VM over
Pageant-backed PuTTY SSH. The `--%` marker keeps PowerShell from rewriting the
quoted WSL command:

```powershell
wsl -d gatherlink-dev --% bash -lc 'printf "%s\n" "#!/bin/sh" "exec /mnt/c/Program\ Files/PuTTY/plink.exe -batch -agent -hostkey <host-key-fingerprint> \"\$@\"" > /tmp/gatherlink-plink-vm-a.sh; chmod +x /tmp/gatherlink-plink-vm-a.sh; cd <wsl-gatherlink-checkout>; GIT_SSH=/tmp/gatherlink-plink-vm-a.sh git push ssh://gatherlink@<vm-a-management-ip>/home/gatherlink/repos/gatherlink.git HEAD:refs/heads/<branch>'
```

On the VM, refresh the working tree from the pushed branch:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\invoke_gatherlink_vm.ps1 -Name gatherlink-vm-a -HostKey "<host-key-fingerprint>" -RemoteCommand "cd /home/gatherlink/src/gatherlink && git fetch origin && git reset --hard origin/<branch> && .venv/bin/pip install -e ."
```

## Gatherlink Configs

The committed Hyper-V VM configs are:

```text
configs/hyperv/two-vm-node-a.json
configs/hyperv/two-vm-node-b.json
```

They use the three private LANs as independent Gatherlink carrier paths:

```text
path-a  10.91.1.11:56001 <-> 10.91.1.12:57001
path-b  10.91.2.11:56002 <-> 10.91.2.12:57002
path-c  10.91.3.11:56003 <-> 10.91.3.12:57003
```

Node A listens for app UDP traffic on `127.0.0.1:55180`. Node B delivers the
decapsulated UDP payload to `127.0.0.1:51820`, where the acceptance runner starts
`tools/udp_probe.py receive`.

## Traffic Shaping

Traffic shaping is done inside the Debian guests with `tc`, through the same
Pageant-backed VM invoke path:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\apply_gatherlink_vm_shape.ps1 -Name gatherlink-vm-a -HostKey "<vm-a-host-key>" -Interface path-a -Rate 3mbit -Delay 10ms -Loss 0.5%
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\apply_gatherlink_vm_shape.ps1 -Name gatherlink-vm-a -HostKey "<vm-a-host-key>" -Interface path-a -Clear
```

The helper intentionally changes only the selected VM interface. For asymmetric
tests, apply different rates on opposite VM egress interfaces.

## One-Command Acceptance From WSL

Run the current Hyper-V acceptance flow from WSL with:

```bash
tools/hyperv/run_gatherlink_vm_acceptance.sh \
  --host-key-a "<vm-a-host-key>" \
  --host-key-b "<vm-b-host-key>"
```

For repeatable runs without putting host-local values in command history, copy
the example inventory to an ignored location and edit it:

```bash
mkdir -p .gatherlink/hyperv-vm-acceptance
cp tools/hyperv/inventory.example.env .gatherlink/hyperv-vm-acceptance/inventory.env
$EDITOR .gatherlink/hyperv-vm-acceptance/inventory.env
tools/hyperv/run_gatherlink_vm_acceptance.sh \
  --inventory .gatherlink/hyperv-vm-acceptance/inventory.env
```

Pass `--ip-a` and `--ip-b`, or set `HYPERV_VM_A_IP` and `HYPERV_VM_B_IP` in the
ignored inventory, to avoid the Hyper-V PowerShell IP resolver. If those
addresses are omitted, the Bash runner uses the small Windows resolver helper
only to discover the current DHCP management addresses. The sync, service
control, shaping, traffic, monitoring, and report flow run from WSL through
Pageant-backed PuTTY `plink`.

The runner:

- syncs source by Git into each VM-local bare repository
- optionally rebuilds the Python virtualenv and Rust PyO3 dataplane
- validates both Hyper-V configs
- starts both managed Gatherlink services
- sends an exact packet smoke
- applies a named shaping profile and sends duration traffic
- fails and recovers each of the three paths
- captures status and monitor snapshots
- fails when duration delivery falls below the configured threshold
- fails when any of the three paths is absent from source transmit or sink
  receive counters
- fails when diagnostics JSONL is missing lifecycle/counter events
- verifies services are stopped after cleanup unless `--keep-running` is set
- prunes stopped process-managed registry records after a completed run
- writes a report under `.gatherlink/hyperv-vm-acceptance/`
- closes services unless `--keep-running` is set

Useful options:

```bash
tools/hyperv/run_gatherlink_vm_acceptance.sh \
  --inventory .gatherlink/hyperv-vm-acceptance/inventory.env \
  --shape-profile asymmetric \
  --duration 30 \
  --min-delivery-ratio 0.90
```

Available shaping profiles are `clean`, `asymmetric`, `lossy`, `latency`, and
`none`. `--soak SECONDS` is an alias for a longer duration run, for example
`--soak 300`.

The Windows wrapper remains available for quick manual checks:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\run_gatherlink_vm_acceptance.ps1 -HostKeyA "<vm-a-host-key>" -HostKeyB "<vm-b-host-key>"
```

Prefer the WSL/Bash runner for anything that may become generally useful outside
this Windows host.

## VM Creation

Create the two VMs with:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\tools\hyperv\create_gatherlink_vms.ps1
```

The script creates `gatherlink-vm-a` and `gatherlink-vm-b` when they are absent.
If a VM with one of those names already exists, the script reuses it and does
not overwrite disks or adapters.

The VMs boot from the downloaded Debian netinst ISO first. Use Hyper-V console
access for the initial Debian install, then remove or deprioritize the DVD boot
entry after installation if desired.

When using the cloud-image path, the VMs boot directly from the generated Debian
cloud VHDX and the attached NoCloud seed ISO. First boot may take a few minutes
while cloud-init updates packages and prepares `/home/gatherlink`.

For consistent interface identification, the script assigns static MAC
addresses to the lab NICs. Do not reuse these MAC addresses for other Hyper-V
VMs on the same host.

## Addressing

Use DHCP on the `External Network` NIC.

Use static addresses on the three path NICs:

```text
path A:
  gatherlink-vm-a 10.91.1.11/24
  gatherlink-vm-b 10.91.1.12/24

path B:
  gatherlink-vm-a 10.91.2.11/24
  gatherlink-vm-b 10.91.2.12/24

path C:
  gatherlink-vm-a 10.91.3.11/24
  gatherlink-vm-b 10.91.3.12/24
```

Do not set default gateways on the path NICs. The only default route should be
through the internet/management NIC.
