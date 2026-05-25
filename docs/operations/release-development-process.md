# Release Development Process

This is the reusable release-building guide. Roadmaps can point here instead of
copying the same process text into every release-specific document. The standing
implementation rules live in `docs/operations/development-discipline.md`; use
that document together with this process when building release slices.

## Three-Pass Rule

Every major implementation slice should be built in three passes.

Pass 1 is the "make it useful" pass:

- implement the feature completely enough to exercise the real behavior
- keep the slice narrowly scoped
- add focused unit tests
- add the relevant lab, VM, or integration check
- update docs when behavior or usage changes
- prove both happy path and fail-closed behavior

Before pass 2, stop and check boundaries:

- Rust should only own packet execution, counters, replay, crypto, queues,
  sockets, and cheap scheduling
- Python should own meaning, config, policy, orchestration, helpers,
  diagnostics, provisioning, and operator output
- helper concerns should stay inside helper code
- platform-specific calls should go through the Debian compatibility backend
- configs, runtime DTOs, diagnostics, and tests should live in the right
  directories
- temporary glue should not become accidental architecture

Pass 2 is the "make it clean" pass:

- reduce duplication
- tighten names and file placement
- simplify data flow
- improve test clarity
- harden edge cases discovered in pass 1
- remove dead scaffolding
- keep responsibilities siloed

Before pass 3, run the same boundary check again.

Pass 3 is the "make it stay clean" pass:

- re-read the changed files as if reviewing someone else's code
- verify failure behavior, diagnostics, and redaction
- run the relevant tests and labs again
- check that docs match the final behavior
- commit only the coherent slice

Do not compress the three passes into one large drift-prone edit. The point is
to let the code settle, then deliberately improve it twice while protecting the
architecture.

## Verification Expectations

Pick the smallest useful verification set for the slice, then broaden it before
declaring the slice done:

- Rust/protocol/dataplane changes: `cargo test` plus relevant lab or VM smoke
- Python/config/helper changes: `.venv/bin/pytest -q` plus helper CLI or lab
  smoke
- Scheduler changes: unit tests, simulation tests, and at least one multipath
  lab or VM run
- Security/envelope changes: Rust crypto tests, Python security tests,
  encrypted lab/config run, and invalid-packet silent-drop checks
- Diagnostics changes: event DTO/bus/sink tests plus a runtime smoke proving
  events are emitted and do not block
- Docs-only changes: stale-reference search and links/path sanity checks

## Release Notes Gate

Starting with v0.9.2, every release must have a matching release-note file in
`docs/releases/` before it can be tagged. Release notes should be compact and
clean, but as complete as possible for the release scope.

Each release note should include:

- what changed since the previous release
- what was tested, including any skipped or waived gates
- supported platform reality
- security posture and audit status
- known limits and intentionally deferred work
- links or references to the matching roadmap, release checklist, and evidence
  docs when the detail belongs there

Do not duplicate long benchmark tables, roadmap prose, or policy text in the
release note. Summarize the outcome and point to the canonical evidence docs.
Do not tag a release while the matching release note is missing, stale, or more
optimistic than the code and test evidence.

## Boundary Review Questions

Use these questions in code review and release readiness passes:

- Is Rust still executing compact facts rather than making product policy?
- Is Python still the source of operator meaning, config, helper behavior, and
  diagnostics?
- Did any lab code become production behavior by accident?
- Did any helper behavior leak into core?
- Did any source file gain unrelated release-note or historical-report content?
- Are secrets, hostnames, VM inventory, and local keys absent from committed
  docs and reports?
- Are stale TODOs either removed or tied to a named roadmap/follow-up?
- Does every new operator-facing behavior have structured diagnostics?
