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
- service close leaves no orphan Gatherlink process
- the generated report lists commands run and pass/fail state
