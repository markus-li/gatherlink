# Real VM Acceptance Harness

This directory contains the v1 real-VM acceptance harness. It is deliberately
plain Bash plus SSH so every action is auditable before it touches a VM.

Default mode is a dry run:

```bash
tools/vm_acceptance/run_acceptance.sh --dry-run
```

Dry-run mode renders node configs, validates them locally, creates a report
directory, and records the commands that would run. It does not contact VMs.

To execute later, copy the inventory template to an ignored path, replace all
placeholders, review the rendered configs, and pass `--execute`:

```bash
mkdir -p .gatherlink/vm-acceptance
cp tools/vm_acceptance/inventory.example.env .gatherlink/vm-acceptance/inventory.env
$EDITOR .gatherlink/vm-acceptance/inventory.env
tools/vm_acceptance/run_acceptance.sh \
  --inventory .gatherlink/vm-acceptance/inventory.env \
  --out .gatherlink/vm-acceptance/run-001 \
  --execute
```

The harness performs the v1 operator path:

1. Render node A and node B configs from templates.
2. Validate configs through `gatherlink config validate`.
3. Prepare VM work directories over SSH.
4. Sync the repository to each VM.
5. Build/install the Python package in a VM-local virtual environment.
6. Upload configs.
7. Start managed Gatherlink services with JSONL diagnostics.
8. Send UDP traffic through node A and receive on node B.
9. Query service status.
10. Capture one-shot service monitor output.
11. Validate diagnostics JSONL with `tools/vm_acceptance/validate_jsonl.py`.
12. Flap and recover one configured path when the VM exposes that interface.
13. Close services and collect reports.

Reports are written under `.gatherlink/vm-acceptance/` by default and are
ignored by git. Do not commit inventories with private hostnames, usernames,
keys, or generated secret material.

The committed example inventory contains deterministic authenticated session
keys only so `--dry-run` can prove the rendered configs are valid. `--execute`
refuses those example keys; replace them with generated session material before
real VM use.
