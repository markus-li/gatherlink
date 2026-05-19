# Real VM Acceptance

## Purpose

The WSL two-distro gate is the fast MVP acceptance path. V1 also needs a real
VM acceptance path because WSL shares enough host networking behavior that it
does not prove deployment realism.

Real VM acceptance should use two Debian VMs with distinct network identities.
Later it can expand to three VMs when secure relay sessions are implemented.

## What Real VMs Prove

Compared with WSL, real VMs prove:

- real host-to-host UDP
- distinct kernel network namespaces
- actual route lookup and interface binding
- VM bridge or virtual switch behavior
- firewall/NAT behavior where present
- more realistic latency, queueing, MTU, jitter, and loss
- service lifecycle on independent machines

## V1 Target

The v1 acceptance target is:

1. Create or start two Debian VMs.
2. Install Gatherlink dependencies.
3. Copy or clone the Gatherlink checkout.
4. Build/install Gatherlink.
5. Generate or install static/session provisioning material.
6. Write node A and node B configs.
7. Validate both configs.
8. Start both managed Gatherlink services.
9. Send UDP traffic from node A to node B.
10. Verify service status, monitor counters, and diagnostics JSONL.
11. Flap or block one path when the VM network can model more than one path.
12. Verify degraded delivery and recovery.
13. Stop both services.
14. Collect logs, diagnostics, configs with secrets removed, and a summary
    report.

## Automation

Build deploy scripts for this testing under:

```text
tools/vm_acceptance/
```

Build the scripts, config templates, report format, and runbook now. Do not ask
for VM access as part of this implementation work, and do not try to contact
real VMs yet. The project owner will provide VM access later.

Current harness:

- `tools/vm_acceptance/run_acceptance.sh`
- `tools/vm_acceptance/inventory.example.env`
- `tools/vm_acceptance/config-node-a.json.template`
- `tools/vm_acceptance/config-node-b.json.template`
- `tools/vm_acceptance/README.md`
- `tools/hyperv/run_gatherlink_vm_acceptance.sh` for the prepared Windows
  Hyper-V two-Debian-VM lab
- `tools/hyperv/run_socks5_vm_acceptance.sh` for the SOCKS5-over-Gatherlink and
  TCP-forward-over-Gatherlink helper proofs against the status HTTP helper on
  the peer VM
- `tools/hyperv/run_wireguard_vm_acceptance.sh` for the WireGuard helper
  endpoint-plan proof and UDP transport proof against the peer-side WireGuard
  target port
- `tools/hyperv/run_gatherlink_vm_soak.sh` for the prepared one-hour v1 soak
  command; do not run the soak casually because it is intentionally long

The harness defaults to `--dry-run`, which renders configs, validates them
locally, and writes the planned command transcript without contacting VMs.
`--execute` is required for SSH/SCP actions and refuses placeholder or committed
example static keys.

The Hyper-V runner is the currently proven concrete VM path. It runs from WSL,
syncs source by Git, starts both managed services, sends traffic across three
private VM paths, applies degradation/recovery, validates monitor counters and
diagnostics JSONL, and cleans up service registry records.

The stream-helper Hyper-V runner proves these helper paths specifically:

```text
SOCKS client on VM A
  -> SOCKS5 helper on VM A
  -> Gatherlink UDP service transport
  -> per-path Gatherlink carrier sockets
  -> stream-exit helper on VM B
  -> status HTTP helper on VM B

HTTP client on VM A
  -> TCP forward helper on VM A
  -> Gatherlink UDP service transport
  -> per-path Gatherlink carrier sockets
  -> stream-exit helper on VM B
  -> status HTTP helper on VM B
```

This is a real Gatherlink tunnel test. The helper test must not be satisfied by
`--lab-direct` or by connecting directly to the peer target.

The WireGuard Hyper-V runner proves the v1 WireGuard helper contract. It does
not create WireGuard interfaces or own WireGuard routes/firewall state. Instead
it verifies that the helper renders the correct peer `Endpoint` and that UDP
payloads sent to that endpoint traverse real Gatherlink carrier sockets and
exit at the configured peer-side WireGuard UDP target.

The soak wrapper uses the same production runner path with a longer duration.
For v1, one hour is the default soak length:

```bash
tools/hyperv/run_gatherlink_vm_soak.sh \
  --inventory tools/vm_acceptance/inventory.local.env \
  --skip-build
```

Document the runbook here and keep generated reports under an ignored output
directory such as:

```text
.gatherlink/vm-acceptance/
```

For v1, prefer simple Bash plus SSH scripts over Ansible.

Reasons:

- fewer dependencies for personal/lab users and small sites
- easier to inspect when AI generates or updates scripts
- easier to run one step at a time while debugging
- matches the current CLI-first workflow
- avoids introducing an inventory/configuration-management system before the
  product needs one

Ansible can be reconsidered later if VM acceptance grows into many nodes,
repeatable matrix runs, or hosted CI infrastructure. It should not be required
for v1.

## AI-Assisted Deploy

AI-assisted deploy is allowed, but it must produce auditable scripts and config
files rather than hiding behavior behind opaque orchestration.

The AI-assisted workflow should:

- generate plain Bash/SSH commands
- show config files before installing them
- redact secrets in reports
- keep a transcript of commands run
- fail closed on missing dependencies or failed validation
- preserve logs and diagnostics after failure

## Acceptance Result

A v1 real-VM run is healthy when:

- all configs validate
- both services start through normal CLI/service management
- traffic crosses the VM boundary
- monitor counters show transmit and receive activity
- diagnostics JSONL is present and parseable
- one-shot monitor output is captured in the VM report directory
- service close leaves no orphan Gatherlink process
- the generated report lists commands run and pass/fail state

## VM-Gated Feature Checks

These areas are implemented or designed far enough to inspect locally, but they
need to be checked in the VM test environment before treating them as release
quality:

- live rekey automation
- richer trust-root UX
- required DNS tunnel behavior
- QUIC carrier direct path
- QUIC carrier through a Traefik UDP reverse proxy
- HTTP/3 DATAGRAM carrier path
- optional DNS-over-HTTPS and full local DNSSEC validation
- optional WireGuard lifecycle automation
- multi-hop relay policy automation

The first VM pass does not need all of these to block basic packet acceptance.
It should record which checks are proven, which are not configured, and which
need later follow-up.

The QUIC-through-Traefik check is documented in
`docs/labs/quic-traefik-proxy.md`. It proves only UDP-capable layer-4
forwarding. It must not be implemented as HTTP or HTTP/3 reverse proxying.

The HTTP/3 DATAGRAM check is documented in
`docs/labs/http3-datagram-carrier.md`. It proves explicit HTTP/3 datagram
support, not ordinary HTTP/3 request proxying.

VM and lab acceptance must include direct no-proxy carrier cases even though the
recommended public deployment posture is to place Gatherlink behind
Cloudflare Spectrum-style TCP/UDP protection and/or Traefik UDP forwarding.
This keeps carrier behavior testable without requiring external services or
accounts.
