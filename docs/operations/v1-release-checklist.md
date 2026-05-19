# V1 Release Checklist

Use this before tagging v1. The release is for Debian personal/lab users and
small sites.

## Must Be True

- docs and source agree on implemented behavior
- no plaintext routing
- no `route_id`
- authenticated sessions are the normal secure path
- static crypto is explicit lab/manual fallback only
- helpers marked active have tests, diagnostics, and user docs
- OS-specific behavior runs through the Debian compatibility layer
- REST/status helper behavior is experimental, local by default, and safe to
  leave disabled

## Source Checks

```bash
cargo test
.venv/bin/pytest -q
.venv/bin/ruff check python tests tools
```

Run targeted tests again for every changed area. Do not rely on the broad test
set alone when the change affects helpers, crypto, relay, scheduling, or
diagnostics.

## Lab Checks

Run the relevant local labs for the changed behavior and at least these v1
baselines:

```bash
gatherlink lab helpers-smoke
gatherlink lab rust-smoke configs/lab/local-dual-path.json
gatherlink lab rust-smoke configs/lab/local-dual-path-encrypted.json
gatherlink lab rust-smoke configs/lab/local-three-path.json
```

For scheduler or path changes, include shaped path profiles and degradation /
recovery checks.

## VM Acceptance

Before v1:

- run real two-VM Debian acceptance
- record config used on both VMs
- record commands run
- record pass/fail output
- record whether live rekey automation, trust-root UX, DNS tunnel, DoH/full
  local DNSSEC, WireGuard lifecycle automation, and multi-hop relay policy
  automation were proven, skipped, or deferred

Current status:

- the Hyper-V two-Debian-VM acceptance runner exists and has passed on the
  prepared Windows host
- DNS Gatherlink-tunnel upstream support is implemented in code, but the final
  v1 report still needs a VM run proving DNS queries traverse Gatherlink rather
  than only local test doubles
- soak remains a separate tag gate

The VM tooling should be automated with simple scripts in the repo. The VMs
themselves are provided externally; do not require repo docs to contain private
access details.

## Soak

Run an operator soak in a real environment before tagging:

- multiple hours minimum
- longer if scheduler, relay, or crypto changed late
- path degradation and recovery during the run
- helpers used in their intended mode
- monitor output captured before, during, and after degradation

## Docs Checks

Review:

- `docs/README.md`
- `docs/user/README.md`
- `docs/user/troubleshooting.md`
- `docs/operations/v1-operator-runbook.md`
- `docs/operations/v1-troubleshooting-guide.md`
- `docs/operations/diagnostics-dictionary.md`
- `docs/user/config-cookbook.md`
- `docs/reports/v1-code-audit-followups.md`
- `docs/project-living-assessment.md`

Close or update every item in `docs/reports/v1-code-audit-followups.md` before
tagging v1.

## Package And Publish

For v1.1 and later packaging work:

- build packages from clean source
- publish package artifacts through GitHub release tooling
- generate the GitHub Wiki user-doc payload from `docs/user/`
- verify the Wiki payload matches the committed docs source
- link release notes to the matching docs commit or tag

Do not manually edit Wiki pages in ways that diverge from the repository docs.

## Tag Conditions

Tag v1 only after:

1. source checks pass
2. local labs pass
3. VM acceptance passes or explicitly records allowed skips
4. soak has no unresolved blocking failures
5. docs match behavior
6. release notes say Debian is the tested platform
7. any remaining limitations are explicit and not hidden in stale TODOs
