# Development Discipline

This document records the working rules for Gatherlink implementation. It is
written for human developers as much as for automation: every release slice
should leave the project easier to reason about than it found it.

## Source Of Truth

Read the active release roadmap before implementing. Follow the documents that
roadmap names as source of truth. If code and docs disagree, either fix the code
or update the docs in the same slice so the disagreement does not survive.

Roadmaps must distinguish status clearly:

- `planned`: documented intent, not implemented yet
- `implemented`: code exists, but it may not be proven enough to rely on
- `unit-tested`: focused unit tests passed for the code path
- `lab-tested`: local lab or integration checks passed for the behavior
- `VM-tested`: VM acceptance or VM benchmark checks passed where required
- `fully tested`: the relevant unit, lab, VM, and release-gate checks passed

Do not mark a feature fully done because the code exists. Record partial proof
plainly so later developers know what still needs checking.

## Ownership Boundaries

Keep the core split intact:

- Python owns config, policy, orchestration, helpers, diagnostics, provisioning,
  scheduler intelligence, control semantics, and operator explanations.
- Rust owns compact packet execution, sockets, counters, AEAD/replay, frame
  parsing, dedupe, batching, fragmentation, bounded queues, and cheap compiled
  scheduling primitives.

Rust must not grow business policy, helper behavior, DNS policy, environment
policy, peer strategy, product routing meaning, or semantic control-plane
branches. If Rust needs to act quickly, Python should compile small primitive
facts for Rust to execute.

Lab code should test production behavior. It must not become the implementation
of production behavior. Helper code stays in helpers. OS-specific behavior goes
through the Debian platform backend unless a future roadmap explicitly adds a
new platform backend.

## Packet And Security Rules

Never reintroduce `route_id`. Relay/routing behavior uses the documented
outer-hop/session model and authenticated context.

Do not add hidden reliability to ordinary UDP payloads. Ack/retry behavior is
allowed only for explicit Gatherlink internal metadata/control services, with
separate counters and diagnostics.

Invalid secure packets should be silent on the network and counted locally.
Endpoint tuple updates require authenticated traffic proving the peer. Plaintext
or static-lab behavior must stay explicit and warning-heavy where it exists.

## Implementation Slices

Work in small, coherent slices:

1. Inspect the roadmap and current code.
2. Implement one useful behavior.
3. Add focused tests.
4. Update docs and roadmap status.
5. Run the relevant test/lab set.
6. Commit the coherent slice before moving on.

For major features, use the three-pass release process in
[`docs/operations/release-development-process.md`](release-development-process.md): make it useful, make it
clean, then make it stay clean. Between passes, review file placement and
responsibility boundaries.

## Testing Expectations

Choose tests based on the behavior changed:

- Python/config/helper changes: focused Python tests, then broader Python tests.
- Rust/protocol/dataplane changes: Rust tests, formatting checks, and relevant
  lab smoke tests.
- Scheduler changes: unit tests, simulation/report checks, local multipath lab,
  and VM checks before calling the behavior fully tested.
- Security changes: Rust crypto/protocol tests, Python security tests,
  encrypted lab checks, and invalid-packet silent-drop checks.
- Diagnostics changes: event/bus/sink tests plus a runtime smoke proving events
  are emitted without blocking.
- Docs-only changes: stale-reference search and path/link sanity checks.

When a lab or VM check is not run, say so in the roadmap or final report. Do not
let a skipped check look like a passed check.

## Performance Work

Performance changes must be evidence-driven. Do not tune constants randomly.
Record the offered rate, delivered rate, drops, queue pressure, path split,
Linux counters, CPU observations, and test topology when making performance
claims.

MPTCP and WireGuard are sources of useful lessons, not compatibility promises.
Only adopt the parts that fit Gatherlink's UDP transport scope and documented
Python/Rust boundary.

## Commit Hygiene

Commit as work is completed. Keep commits scoped to the feature, tests, and docs
for that slice. Do not mix unrelated refactors with feature work. Before merging
to main, clean history into public commits that explain the project clearly.
