# Release Checklist

Use this before tagging a release. The current product target is Debian
personal/lab users and small sites.

## Must Be True

- docs and source agree on implemented behavior
- no plaintext routing
- routing uses authenticated session/control context and relay-hop state
- authenticated sessions are the normal secure path
- static crypto is explicit lab/manual fallback only
- helpers marked active have tests, diagnostics, and user docs
- OS-specific behavior runs through the Debian compatibility layer
- REST/status helper behavior is experimental, local by default, and safe to
  leave disabled
- security docs state the real security posture, including that there has
  been no external security audit unless that changes before release

## Source Checks

```bash
cargo fmt -- --check
cargo test --workspace
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/black --check .
python3 -m compileall -q python tests tools
```

Run targeted tests again for every changed area. Do not rely on the broad test
set alone when the change affects helpers, crypto, relay, scheduling, or
diagnostics.

## Lab Checks

Run the relevant local labs for the changed behavior and at least these
baselines:

```bash
gatherlink lab helpers-smoke
gatherlink lab rust-smoke configs/lab/local-dual-path.json
gatherlink lab rust-smoke configs/lab/local-dual-path-encrypted.json
gatherlink lab rust-smoke configs/lab/local-three-path.json
gatherlink lab shared-sink-smoke configs/lab/local-dual-path-encrypted.json --count 5
```

For scheduler or path changes, include shaped path profiles and degradation /
recovery checks.

## VM Acceptance

Before release:

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
- DNS Gatherlink-tunnel upstream support is implemented in code and passed in
  the Hyper-V two-VM lab with `tools/hyperv/run_dns_vm_acceptance.sh`
- active SOCKS5/TCP stream-helper behavior passed in the Hyper-V VM lab
- WireGuard endpoint/UDP transport behavior passed in the Hyper-V VM lab with
  `tools/hyperv/run_wireguard_vm_acceptance.sh`
- the three-VM untrusted relay/routing WireGuard proof passed with
  `tools/hyperv/run_relay_wireguard_vm_acceptance.sh`
- release soaks are run with `tools/hyperv/run_gatherlink_vm_soak.sh`; the
  default release gate is a 15-minute clean/non-lossy profile and a 15-minute
  lossy or asymmetric profile. Run 30-minute follow-up soaks for the same
  profile class only if a 15-minute soak fails or shows instability that needs
  a longer confirmation window.

The VM tooling should be automated with simple scripts in the repo. The VMs
themselves are provided externally; do not require repo docs to contain private
access details.

## Soak

Release soaks should record:

- 15 minutes for each required release profile class by default
- 30 minutes for the same profile class if the 15-minute soak fails or is
  inconclusive
- path degradation and recovery during the run
- helpers used in their intended mode
- monitor output captured before, during, and after degradation

## Docs Checks

Review:

- [`docs/README.md`](../README.md)
- [`docs/user/README.md`](../user/README.md)
- [`docs/user/troubleshooting.md`](../user/troubleshooting.md)
- [`docs/operations/operator-runbook.md`](operator-runbook.md)
- [`docs/operations/troubleshooting-guide.md`](troubleshooting-guide.md)
- [`docs/operations/diagnostics-dictionary.md`](diagnostics-dictionary.md)
- [`docs/user/config-cookbook.md`](../user/config-cookbook.md)
- [`docs/reports/v0.9-code-audit-followups.md`](../reports/v0.9-code-audit-followups.md)
- [`docs/project-living-assessment.md`](../project-living-assessment.md)

Confirm [`docs/reports/v0.9-code-audit-followups.md`](../reports/v0.9-code-audit-followups.md) has no unresolved
must-fix items before tagging.

Before tagging any version, run a full documentation stale-wording pass:

- update [`docs/project-living-assessment.md`](../project-living-assessment.md)
  so it names the release being tagged as the current closed baseline
- perform a periodic code-vs-doc audit for the release scope; the structural
  Markdown tests catch navigation and hygiene drift, but they do not prove that
  behavior described in docs still matches source behavior
- check release notes, roadmaps, user guides, operation docs, benchmark docs,
  and report indexes for wording such as `active`, `draft`, `release
  candidate`, `planned`, `pending`, `current roadmap`, or old version numbers
  that no longer match release reality
- keep historical documents historical, but make sure navigation docs point to
  the current release and future pipeline correctly
- check report, research, and lab docs for stale implementation-status wording;
  keep implementation status in roadmaps, release notes, and benchmark ledgers
  instead of research notes or lab setup guides
- ensure directory README files use GitHub-clickable Markdown links for
  repository docs rather than bare code-form `.md` filenames
- run the markdown link test and release-hygiene doctor check after the wording
  pass

## Package And Publish

For packaging work:

- build packages from clean source
- publish package artifacts through GitHub release tooling
- generate the GitHub Wiki user-doc payload from `docs/user/`
- verify the Wiki payload matches the committed docs source
- write or update the matching compact release note in `docs/releases/`
- link release notes to the matching docs commit or tag
- before tagging, read the release note as it will appear on GitHub Releases:
  it must make sense after the tag is pushed, avoid branch-local wording such
  as `active on dev`, avoid stale "latest" phrasing when newer evidence is
  listed below, and clearly separate release evidence from historical tuning
  context

Do not manually edit Wiki pages in ways that diverge from the repository docs.

## Public Repository Setup

Before making the repository public:

- update [`SECURITY.md`](../../SECURITY.md) so it matches the real release posture
- enable GitHub private vulnerability reporting if it is available for free on
  the repository
- set the repository description and topics
- confirm GitHub detects the AGPL license correctly
- confirm committed files do not mention private repository names, private
  remote URLs, private VM inventories, host-local paths, keys, tokens, or
  generated local state
- keep branch protection light until the initial public branch/history is in
  place, then protect the default branch

## Tag Conditions

Tag only after:

1. source checks pass
2. local labs pass
3. VM acceptance passes or explicitly records allowed skips
4. soak has no unresolved blocking failures
5. docs match behavior
6. release notes say Debian is the tested platform
7. release notes and [`SECURITY.md`](../../SECURITY.md) say whether an external audit has happened
8. any remaining limitations are explicit and not hidden in stale TODOs
9. labs do not own features that production cannot run
10. the matching `docs/releases/` note is compact,
    complete for the release scope, and no more optimistic than the evidence
11. the matching `docs/releases/` note reads correctly as tagged release text,
    without branch-local status wording or stale "latest" language
12. committed content contains no private repository names, private remote URLs,
    private inventories, host-local credentials, or generated local state
13. [`docs/project-living-assessment.md`](../project-living-assessment.md) has been updated for the release
14. a full documentation stale-wording and code-vs-doc behavior pass has been
    completed and checked
15. report, research, lab, and directory README docs have been checked for
    stale status wording and non-clickable Markdown file references
