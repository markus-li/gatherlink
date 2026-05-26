# Documentation Maintenance

This is the canonical maintenance guide for Gatherlink docs. Keep navigation,
canonical ownership, stale-information handling, and writing style rules here
instead of repeating them across roadmap, user, helper, and report files.

## Canonical Homes

If two docs disagree, prefer the more specific canonical home:

- [`docs/protocol/protocol.md`](../protocol/protocol.md) owns wire layout, frame shape, service ids,
  aggregation, and fragmentation.
- [`docs/protocol/security.md`](../protocol/security.md) and
  [`docs/protocol/relay-session-lifecycle.md`](../protocol/relay-session-lifecycle.md) own secure transport, crypto,
  replay, stealth receive, handshake posture, and relay-hop security.
- [`docs/protocol/runtime-session-model.md`](../protocol/runtime-session-model.md) owns sessions, services, paths,
  receiver indexes, duplicate behavior, and v1/v2 presentation boundaries.
- [`docs/runtime/config-runtime-state.md`](../runtime/config-runtime-state.md) owns human config versus compiled
  runtime state, reload posture, numeric ids, and JSON introspection.
- [`docs/runtime/scheduler.md`](../runtime/scheduler.md) owns scheduler modes, Python/Rust scheduler
  boundaries, and packet-time scheduler behavior.
- [`docs/helpers/helper-priorities.md`](../helpers/helper-priorities.md) owns helper scope, active/deferred
  status, and priority.
- [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md) owns project boundaries and
  permanent ownership rules.
- [`docs/operations/v0.9-operator-runbook.md`](v0.9-operator-runbook.md) and
  [`docs/operations/v0.9-troubleshooting-guide.md`](v0.9-troubleshooting-guide.md) own current operator flows.
- [`docs/benchmarks/README.md`](../benchmarks/README.md) and [`docs/benchmarks/hyperv-performance-log.md`](../benchmarks/hyperv-performance-log.md)
  own benchmark method and measured performance evidence. Benchmark rows should
  keep comparison percentages in the row whenever a baseline exists; use `n/a`
  only when the missing baseline is called out.

Reports and study notes are historical unless a canonical doc cites them or a
release roadmap explicitly promotes their content.

## Navigation Rules

- [`docs/README.md`](../README.md) should help readers find the right document quickly. It
  should not restate the full maintenance policy.
- Directory [`README.md`](../../README.md) files should explain what belongs in that directory and
  list likely entry points.
- Prefer one concise canonical doc per topic.
- Do not create companion `-full.md` copies. Expand the canonical doc or split
  by a clearer subject name.
- When adding a doc that is a likely entry point, update [`docs/README.md`](../README.md) and
  the directory README.
- When a feature doc depends on a broader rule, summarize only the local
  consequence and link to the canonical rule.

## Duplication Rules

Do not duplicate:

- packet layouts
- crypto/security rules
- helper active/deferred status
- architecture ownership boundaries
- permanent exclusions
- release acceptance policy
- benchmark result tables
- user-doc writing rules
- dependency-selection policy

If a non-canonical doc needs the same idea, use one sentence plus a link.

## Volatile Facts

Durable docs should not record volatile totals or local state unless the doc is
a dated release note, acceptance report, or benchmark report.

Avoid durable copies of:

- test counts
- file counts
- generated artifact counts
- local report paths outside dated benchmark/release evidence
- personal machine paths
- VM inventories
- hostnames, keys, or secrets
- raw command dumps without interpretation
- dependency popularity claims that may age

Record commands, scope, pass/fail status, and the canonical place to rerun or
inspect evidence instead.

## TODOs

Implementation TODOs are allowed when they are searchable and tied to a clear
feature area, roadmap, audit follow-up, or release gate. Remove them when the
work is done. Do not let resolved TODOs become pseudo-requirements.

## User Documentation

User documentation must stay short, step-by-step, and scenario-based.

- Write for common real uses, not every possible feature.
- Split usage by helper or workflow, especially SOCKS5 and WireGuard.
- Link to [`docs/user/config-cookbook.md`](../user/config-cookbook.md) for config patterns.
- Link to [`docs/operations/v0.9-operator-runbook.md`](v0.9-operator-runbook.md) for day-to-day operation.
- Keep commands copyable and examples small.
- Explain only what the user needs to run, check, and stop the service.
- Put troubleshooting near the user path: status, logs, monitor, diagnostics.
- Mention current platform testing plainly: Debian tested, most Linux expected.
- Ask users to report bugs as GitHub issues.
- Move implementation rationale to design docs, not user docs.

## Dependency Claims

Library-selection policy lives in [`docs/operations/library-selection.md`](library-selection.md).
Feature docs may name a selected dependency, but they should avoid timeless
maintenance/popularity claims unless the library-selection doc or a dated
decision record supports them.
