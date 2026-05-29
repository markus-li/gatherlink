# Project Living Assessment

Last updated: 2026-05-27

This is the current release-health assessment for Gatherlink. It is not a
protocol spec, implementation diary, or historical milestone tracker. Keep it
short enough that it can be trusted during release work.

## How To Maintain This File

Update this file after meaningful architecture, protocol, runtime, helper, VM,
or release-gate changes.

Rules:

- describe the current project state, not every step that led here
- keep detailed protocol, helper, lab, and roadmap decisions in their owning
  docs and link to them instead of duplicating them here
- record release gates only when there is runnable proof, a reviewed report, or
  an explicit deferral
- do not keep counts, inventories, old milestone checklists, or implementation TODO
  copies here
- if a finding changes release readiness, say whether it is release blocker,
  active-roadmap work, or future pipeline work

Reference docs:

- [`docs/README.md`](README.md)
- [`docs/architecture/architecture-contract.md`](architecture/architecture-contract.md)
- [`docs/protocol/protocol.md`](protocol/protocol.md)
- [`docs/protocol/security.md`](protocol/security.md)
- [`docs/protocol/runtime-session-model.md`](protocol/runtime-session-model.md)
- [`docs/protocol/control-context.md`](protocol/control-context.md)
- [`docs/protocol/relay-session-lifecycle.md`](protocol/relay-session-lifecycle.md)
- [`docs/runtime/config-runtime-state.md`](runtime/config-runtime-state.md)
- [`docs/helpers/helper-priorities.md`](helpers/helper-priorities.md)
- [`docs/labs/lab-demo.md`](labs/lab-demo.md)
- [`docs/operations/testing-strategy.md`](operations/testing-strategy.md)
- [`docs/operations/documentation-maintenance.md`](operations/documentation-maintenance.md)
- [`docs/benchmarks/README.md`](benchmarks/README.md)
- [`docs/operations/release-checklist.md`](operations/release-checklist.md)
- [`docs/reports/v0.9.2-roadmap.md`](reports/v0.9.2-roadmap.md)
- [`docs/releases/v0.9.4.md`](releases/v0.9.4.md)
- [`docs/reports/v0.9.3-roadmap.md`](reports/v0.9.3-roadmap.md)
- [`docs/reports/v0.9.4-roadmap.md`](reports/v0.9.4-roadmap.md)
- [`docs/reports/v0.9.5-roadmap.md`](reports/v0.9.5-roadmap.md)
- [`docs/reports/future-roadmap-pipeline.md`](reports/future-roadmap-pipeline.md)

## Current Judgement

Gatherlink v0.9, v0.9.1, v0.9.2, and v0.9.3 are closed release baselines. The
current tagged release is `v0.9.3`. V0.9.4 work on `dev` has completed the
focused performance follow-up implementation slice and is in final
release-cleanup posture, with running notes in
[`docs/releases/v0.9.4.md`](releases/v0.9.4.md) and scope in
[`docs/reports/v0.9.4-roadmap.md`](reports/v0.9.4-roadmap.md).

V0.9.4 is the public-readiness and installability release after the v0.9.3
adaptive-performance baseline. It focuses on Debian packaging, first-run setup,
secure local REST API behavior, config migration, public docs, support intake,
release-gate packaging evidence, and the promoted focused performance follow-up
for endpoint overhead and WireGuard-over-Gatherlink behavior.

Use the owning docs for details:

- architecture and ownership: [`docs/architecture/architecture-contract.md`](architecture/architecture-contract.md)
- protocol and security: [`docs/protocol/protocol.md`](protocol/protocol.md) and
  [`docs/protocol/security.md`](protocol/security.md)
- runtime/session model: [`docs/protocol/runtime-session-model.md`](protocol/runtime-session-model.md)
- helper scope: [`docs/helpers/helper-priorities.md`](helpers/helper-priorities.md)
- running release notes: [`docs/releases/v0.9.4.md`](releases/v0.9.4.md)
- latest closed release evidence: [`docs/releases/v0.9.3.md`](releases/v0.9.3.md) and
  [`docs/reports/v0.9.3-roadmap.md`](reports/v0.9.3-roadmap.md)
- active release planning: [`docs/reports/v0.9.4-roadmap.md`](reports/v0.9.4-roadmap.md)
- future OpenWrt planning: [`docs/reports/v0.9.5-roadmap.md`](reports/v0.9.5-roadmap.md)
- future roadmap pipeline: [`docs/reports/future-roadmap-pipeline.md`](reports/future-roadmap-pipeline.md)
- release evidence: [`docs/releases/v0.9.0.md`](releases/v0.9.0.md), [`docs/releases/v0.9.1.md`](releases/v0.9.1.md),
  [`docs/releases/v0.9.2.md`](releases/v0.9.2.md), and [`docs/operations/release-checklist.md`](operations/release-checklist.md)

## Release Scope

V0.9 through v0.9.4 are for Debian personal/lab users and small sites. V0.9.4
keeps that product boundary and focuses on installation, first-run setup,
operator APIs, config migration, and public-readiness work.

Supported shape and non-goals are defined in
[`docs/architecture/architecture-contract.md`](architecture/architecture-contract.md). Release-specific user posture is
covered by [`docs/operations/operator-runbook.md`](operations/operator-runbook.md) and
[`docs/user/README.md`](user/README.md).

Deferred beyond the current release unless the next roadmap promotes them: see
[`docs/reports/future-roadmap-pipeline.md`](reports/future-roadmap-pipeline.md).

## Release Gates And Feature State

V0.9 through v0.9.3 evidence is recorded in the release notes, benchmark docs,
roadmaps, and release checklist. V0.9.3 gates are closed in
[`docs/releases/v0.9.3.md`](releases/v0.9.3.md) and
[`docs/reports/v0.9.3-roadmap.md`](reports/v0.9.3-roadmap.md).
V0.9.4 evidence is recorded in [`docs/releases/v0.9.4.md`](releases/v0.9.4.md).

Do not duplicate feature inventories here. Current implementation boundaries
belong in canonical architecture, protocol, runtime, helper, and operations
docs. Historical release contents belong in `docs/releases/`.

## Architecture Health

The strongest part of the project remains the responsibility boundary defined
in [`docs/architecture/architecture-contract.md`](architecture/architecture-contract.md). Protect that boundary during
every future change.

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

1. Clean the v0.9.4 commit history and prepare the release branch before tagging.
2. Keep the focused v0.9.4 performance evidence current if more tuning runs are
   performed before release.
3. Keep v0.9.3 release notes and roadmap as historical evidence, not active
   planning.
4. Promote future-pipeline items into the next release only when they have a
   narrow owner boundary, tests, diagnostics, and realistic lab or VM
   acceptance.
5. Keep performance changes benchmark-led and compared against v0.9.3,
   WireGuard, userspace WireGuard, and raw Gatherlink baselines where relevant.
6. Continue protecting the Python/Rust/helper/lab boundaries before and after
   each implementation pass.

## Latest Closed Assessment

Gatherlink v0.9.3 is tagged for the selected adaptive-performance,
autonomous-rekey, helper-proof, benchmark-discipline, and operator-doc scope.
Full source checks, local labs, VM acceptance, helper acceptance, live rekey
proof, competing-traffic proof, profile export proof, and release hygiene checks
passed on the release branch. Prior clean and lossy soak evidence remains
recorded in the benchmark and release evidence docs; no additional blocking soak
was required for the final docs/tooling-only release slice.
