# Project Living Assessment

Last updated: 2026-05-26

This is the current release-health assessment for Gatherlink. It is not a
protocol spec, implementation diary, or historical milestone tracker. Keep it
short enough that it can be trusted during release work.

## How To Maintain This File

Update this file after meaningful architecture, protocol, runtime, helper, VM,
or release-gate changes.

Rules:

- describe the current project state, not every step that led here
- keep detailed protocol, helper, lab, and roadmap decisions in their canonical
  docs and link to them instead of duplicating them here
- record release gates only when there is runnable proof, a reviewed report, or
  an explicit deferral
- do not keep counts, inventories, old milestone checklists, or implementation TODO
  copies here
- if a finding changes release readiness, say whether it is a tag blocker,
  v0.9.2 completion item, or future pipeline work

Reference docs:

- `docs/README.md`
- `docs/architecture/architecture-contract.md`
- `docs/protocol/protocol.md`
- `docs/protocol/security.md`
- `docs/protocol/runtime-session-model.md`
- `docs/protocol/control-context.md`
- `docs/protocol/relay-session-lifecycle.md`
- `docs/runtime/config-runtime-state.md`
- `docs/helpers/helper-priorities.md`
- `docs/labs/lab-demo.md`
- `docs/operations/testing-strategy.md`
- `docs/operations/documentation-maintenance.md`
- `docs/benchmarks/README.md`
- `docs/operations/v0.9-release-checklist.md`
- `docs/reports/v0.9.2-roadmap.md`
- `docs/reports/future-roadmap-pipeline.md`

## Current Judgement

Gatherlink v0.9 and v0.9.1 are closed release baselines. The current `dev`
branch is tag-ready for the selected v0.9.2 stability and polish scope, subject
to the final maintainer tag/merge step.

V0.9.2 is not a feature-expansion release. It improves stability, cleanliness,
observability, and operator confidence around the already-existing v0.9/v0.9.1
system. The release note is `docs/releases/v0.9.2.md`; detailed evidence is in
`docs/reports/v0.9.2-roadmap.md`.

Use canonical docs for details:

- architecture and ownership: `docs/architecture/architecture-contract.md`
- protocol and security: `docs/protocol/protocol.md` and
  `docs/protocol/security.md`
- runtime/session model: `docs/protocol/runtime-session-model.md`
- helper scope: `docs/helpers/helper-priorities.md`
- current release evidence: `docs/releases/v0.9.2.md` and
  `docs/reports/v0.9.2-roadmap.md`
- release evidence: `docs/releases/v0.9.0.md`, `docs/releases/v0.9.1.md`,
  `docs/releases/v0.9.2.md`, and `docs/operations/v0.9-release-checklist.md`

## Release Scope

V0.9, v0.9.1, and v0.9.2 are for Debian personal/lab users and small sites.

Supported shape and non-goals are defined in
`docs/architecture/architecture-contract.md`. Release-specific user posture is
covered by `docs/operations/v0.9-operator-runbook.md` and
`docs/user/README.md`.

Deferred beyond the v0.9/v0.9.1/v0.9.2 line unless a later roadmap promotes
them: see `docs/reports/future-roadmap-pipeline.md`.

## Release Gates And Feature State

V0.9 and v0.9.1 evidence is recorded in the release notes and release
checklist. V0.9.2 release gates are defined in
`docs/reports/v0.9.2-roadmap.md`.

Do not duplicate feature inventories here. Current implementation boundaries
belong in canonical architecture, protocol, runtime, helper, and operations
docs. Historical release contents belong in `docs/releases/`.

## Architecture Health

The strongest part of the project remains the responsibility boundary defined
in `docs/architecture/architecture-contract.md`. Protect that boundary during
every v0.9.2 and future change.

Current watch points:

- avoid letting helper policy leak into Rust
- avoid making lab-only shortcuts look like production behavior
- avoid adding placeholder modules for deferred helpers or carriers
- keep release docs free of private VM material
- keep user docs short, scenario-based, and Debian-honest
- keep unaudited security posture explicit until external review exists

## Known Limits

Real-world testing is still limited. The project has mainly been tested by the
developer as a practical tool for aggregating a fiber connection and a 5G
connection, plus extensive local, WSL, VM, soak, and simulated-network checks.
More live-user feedback is needed before claiming broader production maturity.

Security has not had an external audit. Present the project honestly as
unaudited software for personal/lab and small-site use.

## Near-Term Priority

1. Tag v0.9.2 from the cleaned release branch after final maintainer review.
2. Keep the v0.9.2 release note compact and no more optimistic than the
   evidence.
3. Use `docs/reports/future-roadmap-pipeline.md` for post-v0.9.2 ideas until a
   new release roadmap is opened.
4. Treat the 15-minute and 30-minute v0.9.2 soaks as passed; the 1-hour soak
   was explicitly waived for this release gate.
5. Keep v0.9, v0.9.1, and v0.9.2 roadmaps historical once the tag is cut.

## Final Assessment

Gatherlink is at v0.9.2 release-candidate state for the selected stability,
observability, scheduler-honesty, helper-proof, and operator-polish scope. Full
source checks, local labs, VM acceptance, helper acceptance, direct carrier VM
smokes, shared-sink proof, relay/WireGuard proof, release-artifact validation,
and the 15-minute and 30-minute soaks have passed on the committed dev branch.
The 60-minute soak is waived for this release, and full autonomous live rekey
automation remains future work rather than a v0.9.2 claim.
