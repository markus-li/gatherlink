# V1 Roadmap

This is the implementation handoff for Gatherlink v1. It is intentionally
direct: use it to guide code work, test work, and review passes without
re-litigating the architecture.

## V1 Target

V1 is for personal/lab users and small sites.

Supported platform for v1:

- Debian only
- CLI first
- local/lab and small-site deployments
- helpers that are already active or functional
- real two-VM Debian acceptance before calling v1 done

Not v1:

- GUI
- hosted account system
- broad platform support beyond Debian
- remote unauthenticated REST management
- plaintext routing
- `route_id`
- full mesh product behavior
- adaptive scheduling marketing

## Read First

Treat these docs as source of truth before changing code:

- `docs/project-living-assessment.md`
- `docs/architecture/architecture-contract.md`
- `docs/architecture/api-surface.md`
- `docs/protocol/security.md`
- `docs/protocol/protocol.md`
- `docs/protocol/runtime-session-model.md`
- `docs/protocol/relay-session-lifecycle.md`
- `docs/runtime/config-runtime-state.md`
- `docs/runtime/state-persistence.md`
- `docs/helpers/helper-priorities.md`
- `docs/labs/real-vm-acceptance.md`
- `docs/operations/testing-strategy.md`

## Standing Rules

- Rust executes compact facts; Python owns meaning.
- No plaintext routing.
- No `route_id`.
- Helpers never become core.
- Static crypto is MVP/lab only, not final v1 security.
- Operator/status output comes from structured facts.
- OS-specific behavior must go through compatibility backends.
- REST is an experimental local helper/control-plane sidecar, not core runtime.

## Multi-Pass Development Rule

Every major v1 slice should be built in three passes.

Pass 1 is the "make it perfect" pass:

- implement the requested feature completely enough to be useful
- keep it narrowly scoped
- add focused unit tests
- add the relevant lab or integration check
- update docs when behavior or usage changes
- make the happy path and fail-closed path work

Between pass 1 and pass 2, stop and check boundaries:

- is Rust only doing packet execution, counters, replay, crypto, queues, and
  cheap scheduling?
- is Python still owning policy, config, orchestration, helpers, diagnostics,
  provisioning, and operator meaning?
- are helper concerns still inside helper code?
- are platform-specific calls behind the Debian compatibility backend?
- are configs, runtime DTOs, diagnostics, and tests in the right directories?
- did any temporary glue become accidental architecture?

Pass 2 is the "make it cleaner" pass:

- reduce duplication
- tighten names and file placement
- simplify data flow
- improve test clarity
- harden edge cases discovered in pass 1
- remove dead scaffolding
- keep responsibilities siloed

Between pass 2 and pass 3, run the same boundary check again.

Pass 3 is the "make it stay clean" pass:

- re-read the changed files as if reviewing someone else's code
- verify failure behavior, diagnostics, and redaction
- run the relevant tests and labs again
- check that docs match the final behavior
- commit only the coherent slice

Do not compress the three passes into one large drift-prone edit. The point is
to let the code settle, then deliberately improve it twice while protecting the
architecture.

## Implementation Order

### Pass Group 1: Stabilize MVP Tests

- Finish and commit the UDP receive retry hardening if it is still pending.
- Run `cargo test` repeatedly enough to catch obvious timing flakes.
- Run `.venv/bin/pytest -q`.
- Run the WSL MVP acceptance gate.

### Pass Group 2: Debian Compatibility Backend

- Add a platform compatibility layer.
- Keep Debian as the only implemented backend for v1.
- Route systemd, journal, `ip`, `ss`, `tc`, `/sys`, capability, and path-layout
  behavior through the backend where practical.
- Do not scatter new OS-specific calls through runtime or helper logic.

### Pass Group 3: Persistence And Secrets

- Formalize Debian paths for config, state, and logs.
- Persist node identity, trust roots, signed bundles, endpoint caches, and
  non-authoritative hints where appropriate.
- Never expose secret key material through status, diagnostics, REST, or JSON
  config output.

### Pass Group 4: Signed Provisioning

- Implement Ed25519 signed topology/provisioning bundles.
- Use canonical JSON for v1.
- Keep provisioning out-of-band and friendly to SSH/file-copy workflows.
- Add tamper, stale-generation, and signature-validation tests.

### Pass Group 5: Authenticated Sessions

- Replace static crypto as the v1 normal path with a WireGuard-like
  authenticated session setup.
- Keep silent drop on invalid packets.
- Keep receiver indexes opaque.
- Add rekey, replay reset, expiry, and golden-vector coverage.
- Leave static crypto as explicit lab/manual mode only.

### Pass Group 6: Secure Relay Sessions

- Implement relay-hop session state from the relay docs.
- Relays are untrusted.
- Relays must not route by plaintext labels.
- Invalid relay packets must not be forwarded and must get no network response.

### Pass Group 7: Experimental REST Helper

- Implement REST as a helper/control-plane sidecar.
- Start it explicitly from CLI.
- Bind to `127.0.0.1` by default.
- Require an explicit danger flag for non-loopback bind.
- Mark it `EXPERIMENTAL`.
- Let read APIs continue while the helper runs.
- Expire write APIs after one hour unless restarted from CLI.
- Return structured facts, not secret material.

### Pass Group 8: Helper V1 Hardening

Bring currently functional helpers to v1 status:

- WireGuard
- SOCKS5 TCP CONNECT
- TCP forward
- DNS
- explicit opt-in time helper

Each helper needs explicit config, managed lifecycle, diagnostics, user docs,
fail-closed defaults, tests, and lab/smoke coverage.

Deferred helpers stay deferred.

### Pass Group 9: Real VM Acceptance Automation

- Add scripts under `tools/vm_acceptance/`.
- Use simple Bash/SSH scripts for v1, not Ansible.
- Build the scripts, configs, report format, and docs now.
- Do not ask for VM access yet.
- Do not try to contact real VMs yet.
- The project owner will provide VM access later.

The scripts should install dependencies, sync the repo or package, write
configs, validate configs, start services, send traffic, flap or block a path
when practical, collect diagnostics, stop services, and write a report.

### Pass Group 10: V1 Acceptance

Before declaring v1:

- `cargo test` passes repeatedly
- `.venv/bin/pytest -q` passes
- helper smoke passes
- WSL MVP acceptance passes
- real two-Debian-VM acceptance passes
- WireGuard over Gatherlink works
- SOCKS5 over Gatherlink works
- DNS helper works
- TCP forward works
- service start/status/monitor/close works
- diagnostics JSONL is parseable and useful
- invalid encrypted packets silent-drop and count locally
- soak testing runs in a real environment
- user docs are short, accurate, and scenario-based

## Code-Chat Instruction Block

Use this when handing the work to a code-building chat:

```text
We are now building Gatherlink v1.

Read `docs/reports/v1-roadmap.md` first, then follow the docs it lists under
"Read First". Treat those docs as source of truth.

Work in three passes for every major slice.

Pass 1: make it perfect. Implement the feature completely enough to be useful,
add focused unit tests, add the relevant lab/integration checks, update docs
when behavior changes, and prove both happy path and fail-closed behavior.

Before pass 2: review file placement and responsibility boundaries. Rust should
only own compact packet execution, counters, crypto, replay, queues, sockets,
and cheap scheduling. Python should own meaning, config, policy, orchestration,
helpers, diagnostics, provisioning, and operator output. Helper code stays in
helpers. OS-specific behavior goes through the Debian compatibility backend.

Pass 2: improve it. Reduce duplication, tighten names, move code to the right
place, simplify data flow, improve tests, harden edge cases, remove temporary
scaffolding, and keep responsibilities siloed.

Before pass 3: run the same boundary and cleanliness review again.

Pass 3: improve it again. Re-read the changed files as a reviewer, verify
failure behavior, diagnostics, redaction, docs, and tests, then commit only the
coherent slice.

Build in the roadmap order:
1. stabilize MVP tests
2. Debian compatibility backend
3. persistence and secrets
4. signed provisioning
5. authenticated sessions
6. secure relay sessions
7. experimental REST helper
8. helper v1 hardening
9. real VM acceptance automation
10. v1 acceptance

For real VM acceptance, build the Bash/SSH tooling, configs, report format, and
docs under `tools/vm_acceptance/`, but do not ask for VM access and do not try
to contact real VMs yet. The project owner will provide access later.

Do not implement GUI, hosted accounts, broad non-Debian platform support,
remote unauthenticated REST management, plaintext routing, `route_id`, or a
full mesh product.
```
