# WSL Two-Distro Lab

## Purpose

This is the repeatable Windows-hosted MVP lab. It uses two Debian WSL
distributions as separate Gatherlink processes so the normal service lifecycle,
status HTTP helper, static AEAD config, three carrier paths, shaping, path
degradation/recovery, diagnostics, and teardown can be tested before moving the
same commands to two full Hyper-V/VirtualBox/VMware Debian VMs.

WSL instances may share the same Windows-side virtual network address depending
on Windows networking mode. For true VM isolation, use two real Debian VMs and
replace the `transport_remote` addresses with the other VM's reachable IP.
For the current WSL-based two-node lab, Gatherlink uses a WSL-private address
shim: `10.88.0.11`/`10.88.0.12` for service traffic plus three independent
carrier LAN pairs under `10.88.1.0/24`, `10.88.2.0/24`, and `10.88.3.0/24`.
Each carrier LAN has distinct source/destination IPs and UDP ports so traffic
can be shaped or dropped independently with `tc`.

## Prepared Instances

Current prepared WSL instances:

```powershell
wsl --list --verbose
```

Expected Gatherlink instances:

- `gatherlink-dev`
- `gatherlink-peer`

Both should have `/home/markus/src/gatherlink` and the project virtualenv.

## Successful Local Setup Summary

The tested WSL setup was:

1. Export the already-working `gatherlink-dev` distro.
2. Import that export as `gatherlink-peer`.
3. Run peer commands as `markus`, because the repo and virtualenv live under
   `/home/markus`.
4. Sync code from `gatherlink-dev` to `gatherlink-peer` with a Git bundle.
5. Add the service and path `/32` addresses to WSL loopback with
   `tools/setup_wsl_private_lan.ps1`.
6. Bind node A service traffic to `10.88.0.11`, node B service traffic to
   `10.88.0.12`, and the three Gatherlink carrier paths to `10.88.1.x`,
   `10.88.2.x`, and `10.88.3.x`.
7. Start both managed services with `gatherlink run start`.
8. Send a UDP probe into node A and receive it from node B.

This proves the managed service lifecycle, static AEAD path, Rust UDP path
transport, private WSL address shim, service monitor counters, JSONL
diagnostics, and clean teardown. It does not prove true VM isolation because WSL
shares one Linux network namespace here.

## Creating The Second Debian Instance

The working second instance was created by cloning the already-working
`gatherlink-dev` WSL distribution. This preserved the Debian users, installed
tooling, Rust/Python build environment, checked-out repo, virtualenv, and GitHub
CLI setup.

From Windows PowerShell:

```powershell
wsl --export gatherlink-dev "$env:USERPROFILE\Documents\gatherlink-dev.tar"
wsl --import gatherlink-peer "$env:USERPROFILE\Documents\gatherlink-peer" "$env:USERPROFILE\Documents\gatherlink-dev.tar"
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink --help"
```

If an earlier attempt created the wrong instance, remove only that instance and
re-import from `gatherlink-dev`:

```powershell
wsl --terminate gatherlink-peer
wsl --unregister gatherlink-peer
wsl --import gatherlink-peer "$env:USERPROFILE\Documents\gatherlink-peer" "$env:USERPROFILE\Documents\gatherlink-dev.tar"
```

After import, keep the peer repo synced from the primary working tree with a Git
bundle. This avoids needing network access or GitHub credentials inside both
instances during local testing:

```powershell
wsl -d gatherlink-dev -- bash -lc "cd /home/markus/src/gatherlink && git bundle create /mnt/c/Users/<windows-user>/Documents/gatherlink-project-orientation.bundle project-orientation"
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && git fetch /mnt/c/Users/<windows-user>/Documents/gatherlink-project-orientation.bundle project-orientation:refs/remotes/bundle/project-orientation && git checkout project-orientation && git reset --hard refs/remotes/bundle/project-orientation"
```

Verify both sides:

```powershell
wsl --list --verbose
wsl -d gatherlink-dev -- bash -lc "cd /home/markus/src/gatherlink && git log --oneline -1 && . .venv/bin/activate && gatherlink config validate configs/examples/windows-two-node-a.json"
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && git log --oneline -1 && . .venv/bin/activate && gatherlink config validate configs/examples/windows-two-node-b.json"
```

Notes:

- Use `-u markus` for `gatherlink-peer` when running project commands. The
  imported distro has the `markus` user and repo under `/home/markus`.
- WSL distributions may share the same `eth0` address. That is acceptable for
  this local process/service smoke path, but it is not a substitute for a true
  two-VM network acceptance test.
- The bundle sync uses `git reset --hard` only inside the disposable peer clone,
  where its purpose is to mirror the primary development checkout exactly.

## WSL Private LAN Shim

Hyper-V virtual switches cannot be attached directly to WSL distributions the
way they can be attached to normal Hyper-V VMs. The two prepared Debian WSL
instances also share the same Linux network namespace, which can be confirmed
with:

```powershell
wsl -d gatherlink-dev -- bash -lc "sudo readlink /proc/1/ns/net"
wsl -d gatherlink-peer -u markus -- bash -lc "sudo readlink /proc/1/ns/net"
```

Both currently report the same namespace id. Because of that, the WSL test path
uses loopback-hosted private addresses. Configure them from Windows:

```powershell
powershell.exe -ExecutionPolicy Bypass -File ".\tools\setup_wsl_private_lan.ps1"
```

That script runs:

```bash
sudo ip addr replace 10.88.0.11/32 dev lo
sudo ip addr replace 10.88.0.12/32 dev lo
sudo ip addr replace 10.88.1.11/32 dev lo
sudo ip addr replace 10.88.1.12/32 dev lo
sudo ip addr replace 10.88.2.11/32 dev lo
sudo ip addr replace 10.88.2.12/32 dev lo
sudo ip addr replace 10.88.3.11/32 dev lo
sudo ip addr replace 10.88.3.12/32 dev lo
```

The aliases are visible to both WSL distros because they share the same network
namespace. The WSL example configs bind node A to `10.88.0.11` and node B to
`10.88.0.12`, so the Gatherlink carrier sockets and test service sockets no
longer use plain `127.0.0.1`.

## WSL Path Shaping

The WSL path shim supports three simultaneous Gatherlink carrier links. Since
both WSL distros share one Linux network namespace, shaping is applied once to
the shared `lo` device and can be done from either WSL instance:

```powershell
wsl -d gatherlink-dev -- bash -lc "cd /home/markus/src/gatherlink && sudo tools/wsl_shape_private_lan.sh apply path-a.ab=3mbit path-a.ba=2mbit path-b.ab=1500kbit path-b.ba=1500kbit path-c.ab=750kbit path-c.ba=1mbit"
```

Direction labels:

- `path-a.ab`: node A to node B, `10.88.1.11:56001` to `10.88.1.12:57001`
- `path-a.ba`: node B to node A, `10.88.1.12:57001` to `10.88.1.11:56001`
- `path-b.ab`: node A to node B, `10.88.2.11:56002` to `10.88.2.12:57002`
- `path-b.ba`: node B to node A, `10.88.2.12:57002` to `10.88.2.11:56002`
- `path-c.ab`: node A to node B, `10.88.3.11:56003` to `10.88.3.12:57003`
- `path-c.ba`: node B to node A, `10.88.3.12:57003` to `10.88.3.11:56003`

The three carrier LANs are intentionally separate even though they all live on
loopback in WSL:

| Carrier | Node A bind | Node B bind | Purpose |
| --- | --- | --- | --- |
| path A | `10.88.1.11:56001` | `10.88.1.12:57001` | fastest/default path |
| path B | `10.88.2.11:56002` | `10.88.2.12:57002` | medium path |
| path C | `10.88.3.11:56003` | `10.88.3.12:57003` | slow/degraded path |

Inspect or clear shaping:

```powershell
wsl -d gatherlink-dev -- bash -lc "cd /home/markus/src/gatherlink && sudo tools/wsl_shape_private_lan.sh show"
wsl -d gatherlink-dev -- bash -lc "cd /home/markus/src/gatherlink && sudo tools/wsl_shape_private_lan.sh clear"
```

Raw UDP sanity check:

```powershell
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && python3 tools/udp_probe.py receive 10.88.0.12:59099 --timeout 5"
wsl -d gatherlink-dev -- bash -lc "cd /home/markus/src/gatherlink && python3 tools/udp_probe.py send 10.88.0.12:59099 raw-private-lan"
```

Expected result: the peer receiver prints `raw-private-lan`.

## Example Configs

The first static-AEAD examples are:

- `configs/examples/windows-two-node-a.json`
- `configs/examples/windows-two-node-b.json`

They use opposite static send/receive keys and different path UDP ports:

- WSL path A: `10.88.1.11:56001` to `10.88.1.12:57001`
- WSL path B: `10.88.2.11:56002` to `10.88.2.12:57002`
- WSL path C: `10.88.3.11:56003` to `10.88.3.12:57003`
- both nodes use the same static `receiver_index` for this MVP static session;
  the send/receive keys are reversed by direction
- node A user UDP listen: `10.88.0.11:55180`
- node B application target: `10.88.0.12:51820`
- node B service listen: `0.0.0.0:0`, an ephemeral local socket used by the
  current Rust userland service abstraction while receive-side target-only
  services are productized further

For two real Debian VMs, change:

- node A `paths[0].transport_remote` to `<node-b-ip>:57001`
- node B `paths[0].transport_remote` to `<node-a-ip>:56001`

Keep targets explicit. Control metadata may assert endpoint expectations later,
but it must not silently set target IP/port.

## Commands

The repeatable WSL MVP acceptance gate is:

```powershell
powershell.exe -ExecutionPolicy Bypass -File ".\tools\run_wsl_mvp_acceptance.ps1"
```

It performs the manual steps below as one check:

- configures the WSL-private service/path addresses
- syncs the peer WSL checkout from the primary checkout with a Git bundle
- validates both static-AEAD node configs
- applies three asymmetric shaped carrier links
- starts both process-managed Gatherlink services
- sends counted UDP payloads from node A to node B
- drops one carrier path, verifies degraded traffic still arrives over the
  remaining paths, then restores shaping and verifies exact counted delivery
- verifies per-path status counters and service monitor output
- verifies service diagnostics JSONL is present and parseable
- closes both services and confirms they are stopped

Use `-ClearShaping` if the run should remove the WSL tc shaping after the
acceptance check. By default shaping is left in place so follow-up manual
traffic tests use the same three-link private LAN profile.

Manual commands are still useful when debugging individual layers.

In Windows Terminal, start node B first:

```powershell
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink run start configs/examples/windows-two-node-b.json --name core.windows-node-b --scheduler-reapply-interval 5"
```

Then start node A:

```powershell
wsl -d gatherlink-dev -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink run start configs/examples/windows-two-node-a.json --name core.windows-node-a --scheduler-reapply-interval 5"
```

Optional status HTTP helpers:

```powershell
wsl -d gatherlink-dev -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink helpers status-http --listen 127.0.0.1:8765"
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink helpers status-http --listen 127.0.0.1:8766"
```

Check managed services:

```powershell
wsl -d gatherlink-dev -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink services status core.windows-node-a"
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink services status core.windows-node-b"
```

Send one packet through node A and receive it at node B:

```powershell
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && python3 tools/udp_probe.py receive 10.88.0.12:51820 --timeout 6"
wsl -d gatherlink-dev -u markus -- bash -lc "cd /home/markus/src/gatherlink && python3 tools/udp_probe.py send 10.88.0.11:55180 hello-two-node"
```

Expected result:

- the peer receiver prints `hello-two-node`
- `gatherlink services monitor core.windows-node-a --once` shows one transmitted
  service/path packet
- `gatherlink services monitor core.windows-node-b --once` shows one received
  service/path packet

Stop services:

```powershell
wsl -d gatherlink-dev -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink services close core.windows-node-a"
wsl -d gatherlink-peer -u markus -- bash -lc "cd /home/markus/src/gatherlink && . .venv/bin/activate && gatherlink services close core.windows-node-b"
```

## Current Limits

- This is prepared for static AEAD and managed service lifecycle testing.
- WSL inter-instance encrypted three-path UDP acceptance is repeatable through
  `tools/run_wsl_mvp_acceptance.ps1`, but true VM traffic acceptance still needs
  a real remote IP pair or a Windows networking mode where each Debian VM has a
  distinct reachable address.
- The status HTTP helper is intentionally read-only and local by default.
