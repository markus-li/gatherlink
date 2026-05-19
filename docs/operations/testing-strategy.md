# Testing Strategy

## Purpose

Gatherlink needs repeatable tests for network behavior, not only unit tests.

Every development change should add or update focused unit tests for the code it
touches. When the change affects runtime behavior, packet movement, config
expansion, helpers, security, scheduling, diagnostics, or labs, also run full
lab/config sets that make sense for that area. Do not rely on unit tests alone
when the feature's real value is cross-process or cross-path behavior.

## Test layers

Use pure Python unit tests, Rust unit tests, protocol encode/decode tests,
integration tests with network namespaces, netem loss/jitter/reorder tests,
bootstrap/DNS tests, and long-running soak tests.

Suggested default development verification:

- Rust/protocol/dataplane changes: `cargo test`, plus relevant path/lab config
  smoke tests
- Python/config/helper changes: `.venv/bin/pytest -q`, plus helper-specific CLI
  or lab configs
- scheduler changes: unit tests, simulation tests, and at least one multi-path
  lab/config run
- security/envelope changes: Rust crypto tests, Python security tests, encrypted
  lab/config run, and invalid-packet silent-drop checks
- docs that affect implementation guidance: update or add tests in the code
  chat when that guidance is implemented

## Rust tests

Cover frame encode/decode, replay windows, dedupe, reorder buffer, MTU
eligibility, weighted round-robin distribution, queue overflow, and invalid
packet silent rejection.

Add golden vectors for compact v1 headers, compact v2 decrypted headers, secure
direct envelopes, and relay-hop envelopes. Golden vectors should pin byte layout,
associated-data construction, replay counter behavior, and failure cases.

Rust production modules should not carry inline `#[cfg(test)] mod tests` blocks.
Put Rust behavior tests in each crate's `tests/` directory instead, even when
the tests exercise one module heavily. This keeps the Python/Rust boundary,
packet executor, protocol encoder, and PyO3 bridge files readable as production
code first. If a future test genuinely needs private internals, prefer exposing a
small crate-private test helper module rather than growing production files with
large embedded tests.

## Python unit tests

Cover config validation, config expansion, path state transitions, carrier
discovery decisions, peer failover/failback, DNS policy matching, domain-set
matching, bootstrap candidate ordering, time quality scoring, and hook debounce.

Helper tests should use fake in-process services first. Each helper should have
diagnostics tests from day one so operator-visible failures do not become
afterthoughts.

DNS helper tests should include:

- DNSSEC good answer
- DNSSEC bogus answer
- unsigned answer under allowed policy
- unsigned answer under require-validation policy
- IDNA input/output cases
- policy-denied responses

## Integration tests

Use Linux namespaces and veth pairs. Scenarios should include two WAN paths,
fixed round-robin, one path down, path warmup, raw UDP blocked with WSS active,
MTU mismatch, receiver metrics loss, peer failover, DNS helper racing, bootstrap
via cache/direct DNS/DoH, and same-subnet distinct gateway validation.

Relay tests must assert that invalid relay packets are not forwarded and do not
produce network responses. They should cover unknown receiver index, AEAD
failure, replay, expired session, stale generation, unauthorized next hop, and
rate-limit failure.

The first runnable integration target is the local dual-path lab documented in
`docs/labs/local-dual-path-lab.md`. Lab setup and shaping tools may use root, but the
two Gatherlink instances must run unprivileged.

## Netem scenarios

Use tc/netem to simulate latency, jitter, packet loss, burst loss, reorder, rate
limiting, blackhole, recovery, and flapping.

## Deterministic tests

Where possible, scheduling tests should use deterministic fake clocks and fake
metrics. Avoid tests that depend on internet availability.

## Public demo tests

The demo should prove UDP payload enters a local virtual port, packets split over
paths, the remote emits original UDP payload, tcpdump shows both paths, path
failure does not kill service, and diagnostics explain decisions.

Before crypto/authentication exists, the demo may use explicit plaintext mode as
documented in `docs/protocol/plaintext-security-mode.md`. That mode must warn loudly in
Python terminal output and logs.
