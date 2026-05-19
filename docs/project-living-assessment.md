# Project Living Assessment

Last updated: 2026-05-20

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
  v0.9.1 completion item, or future pipeline work

Canonical docs:

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
- `docs/operations/v0.9-release-checklist.md`
- `docs/reports/v0.9-roadmap.md`
- `docs/reports/v0.9.1-roadmap.md`
- `docs/reports/future-roadmap-pipeline.md`
- `docs/reports/v0.9-code-audit-followups.md`

## Current Judgement

Gatherlink is past the v0.9 tag and is at v0.9.1 release-candidate state for the
same deliberately narrow Debian personal/lab and small-site scope. The current
tree contains real v0.9.1 implementation slices for DNS-over-HTTPS upstreams,
standard datagram carrier adapters, local release artifacts, lab bundle
generation, topology diffing, session-rotation policy helpers, VM report JSON,
and the guarded experimental local status write endpoint.

The main remaining v0.9.1 work is final release hygiene, not broad product
design. Do not tag v0.9.1 until the local release gate has been rerun from a
clean tree, the local editable install/package metadata reports 0.9.1, release
artifacts build and doctor-check successfully, and the recorded VM/proxy
evidence is retained with the release notes.

The core system has:

- a Rust packet executor for compact UDP frame handling, AEAD, replay
  protection, dedupe, batching, fragmentation, queues, counters, sockets, and
  scheduler facts
- a Python control/runtime layer for config, validation, provisioning,
  lifecycle, helpers, diagnostics, monitoring, release tooling, and operator
  meaning
- authenticated config-facing session setup that compiles down to compact AEAD
  facts for Rust
- relay-hop execution and routing without plaintext routing labels
- production-owned sparse discovery, remote-status, and monitor-cadence control
  paths
- managed helper slices for SOCKS5 TCP CONNECT, TCP forwarding, DNS direct,
  tunnel, and DoH upstreams, WireGuard planning/transport proof, and the narrow
  time helper
- Python-owned QUIC DATAGRAM and HTTP/3 DATAGRAM carrier adapters that wrap
  opaque Gatherlink packets around the Rust UDP packet executor
- local, WSL, Hyper-V VM, relay/routing VM, and soak evidence for the declared
  v0.9 scope, with v0.9.1 local carrier and artifact gates now added

## Release Scope

V0.9 and v0.9.1 are for Debian personal/lab users and small sites.

Supported shape:

- Debian is the tested and supported platform
- CLI operation is the supported operator interface
- Rust owns packet execution and compact facts
- Python owns policy, config, orchestration, helpers, diagnostics, and UX
- UDP services are carried over configured Gatherlink paths
- encryption is the normal production path
- invalid encrypted packets and invalid relay packets are silently dropped on
  the network side
- no plaintext routing is supported
- untrusted peers route only through authenticated outer routing/relay-hop
  context

Project non-goals:

- general SD-WAN behavior
- firewall, NAT, LAN routing, or DPI ownership

These are not normal backlog items. They should stay out of Gatherlink unless
the project is intentionally redefined, because they would blur the packet
transport/helper boundary and turn Gatherlink into a different kind of system.
A narrow Linux endpoint-protection helper may be considered later, but only as
helper-owned integration for explicit Gatherlink endpoint scenarios. It must not
become general firewall, NAT, LAN routing, or SD-WAN policy ownership.

Deferred beyond the v0.9/v0.9.1 line unless a later roadmap promotes them:

- production GUI
- broad platform support beyond Debian
- automatic update/rollback channels
- WSS, TCP/TLS, CONNECT-UDP/MASQUE, and obfuscation carriers
- broad multi-hop relay policy automation beyond the implemented first slice
- optional WireGuard interface lifecycle automation

## Release Gates

Passed or recorded v0.9 evidence:

- Rust workspace tests and formatting checks
- Python lint, formatting, compile, and pytest checks
- local lab Rust smokes across plaintext, encrypted, multipath, IPv6, and shared
  sink shapes
- WSL acceptance gate with encrypted multipath behavior, degradation,
  recovery, monitoring, diagnostics, and clean close
- Hyper-V two-Debian-VM core acceptance
- DNS tunnel upstream VM proof
- active stream-helper VM proof for SOCKS5/TCP helper transport behavior
- WireGuard endpoint/UDP transport VM proof
- relay WireGuard VM proof through an untrusted VM C, proving the
  routing/relay-hop shape
- one-hour operator soak
- local hidden-sink remote-status proof
- three-WSL shared-sink remote-status proof

Added v0.9.1 local evidence in the current tree:

- Rust crate metadata and root Python project metadata are at 0.9.1
- DNS-over-HTTPS helper upstream implementation and tests
- QUIC DATAGRAM and HTTP/3 DATAGRAM carrier adapters with local byte-preserving
  smoke coverage
- three-VM direct carrier and Traefik UDP-forwarded proxy carrier smoke evidence
  for both QUIC DATAGRAM and HTTP/3 DATAGRAM
- bridge tests proving non-UDP carriers fail closed if sent directly to the
  Rust UDP DTO path without Python carrier supervision
- local release artifact tooling for tracked source archives, Python wheels,
  Rust helper binaries, checksums, and `docs/user/` Wiki payloads
- doctor checks for release artifact shape
- VM acceptance JSON report schema and tests
- lab bundle generation/preflight/cleanup planning tests
- guarded `POST /v1/services/{name}/close` status HTTP endpoint with write
  expiry tests

Before tagging `v0.9.1`:

- rerun the full local v0.9.1 release gate from `docs/releases/v0.9.1.md`
- refresh or reinstall the local editable package so `importlib.metadata`
  reports 0.9.1
- build release artifacts with `--version 0.9.1` and validate them with
  `gatherlink doctor --release-artifacts`
- confirm committed files contain no private VM hostnames, addresses, inventory
  paths, keys, or operator-only material
- retain the operator-run VM/proxy carrier evidence referenced by
  `docs/releases/v0.9.1.md`
- tag from a clean source/docs-aligned tree

## Current Feature State

Implemented and in v0.9 scope:

- compact v1/v2 frames
- AEAD envelope and replay protection
- Rust UDP dataplane
- multipath scheduling primitives with live reapply
- batching, fragmentation/reassembly, dedupe, and counters
- PyO3 bridge
- config validation and runtime expansion
- service registry, local monitor, and remote-observability path
- diagnostics bus and JSONL sink
- authenticated session planning/exchange first slice
- signed topology/provisioning first slice
- relay session authorization and hop execution first slice
- relay fabric discovery/health helper slice
- persistence and local secret UX first slice
- SOCKS5 TCP CONNECT helper over Gatherlink UDP transport with companion exit
- TCP forwarding helper over Gatherlink UDP transport with companion exit
- DNS helper direct, Gatherlink-tunnel, and DNS-over-HTTPS upstream paths with
  cache, IDNA, and AD-bit DNSSEC handling
- WireGuard planning/config helper and endpoint/UDP transport proof
- narrow time helper
- local release artifact builder and release-artifact doctor checks
- operator-safe lab bundle generation and scoped cleanup planning
- signed topology diff command
- session rotation policy helper decisions
- experimental status HTTP helper with guarded service-close write endpoint
- QUIC DATAGRAM and HTTP/3 DATAGRAM carrier adapters around opaque Gatherlink
  packets, supervised by Python before Rust sees local UDP endpoints

Implemented but intentionally limited:

- runtime reload currently covers scheduler reapply; broader config reload is
  future/polish
- WireGuard helper does not own interface lifecycle
- DNSSEC support is upstream-AD oriented; full local validation remains optional
  later work
- relay policy automation is a first slice, not a full mesh controller
- REST API is experimental helper/service work, loopback by default, and not
  stable remote management UX
- QUIC/H3 carrier support has local adapter smoke coverage and three-VM
  acceptance coverage for direct carriers plus Traefik UDP-forwarded proxy
  carriers. Impaired-network proxy soak remains separate lab evidence.

Deferred:

- full live rekey wiring beyond the current policy helper
- richer trust-root lifecycle UX beyond topology diffing
- broader diagnostics polish, including carrier connect/reconnect/drop events
- optional DNS full local DNSSEC validation
- optional WireGuard lifecycle automation
- broader multi-hop relay policy automation
- future carrier, obfuscation, package-update, rollback, UI, policy, and
  compatibility work belongs in `docs/reports/future-roadmap-pipeline.md`

## Architecture Health

The strongest part of the project remains the responsibility boundary:

- Rust executes compact packet facts.
- Python decides what those facts mean.
- Helpers stay outside the core packet executor.
- Lab code may enable, accelerate, or prove production hooks, but it must not be
  the only implementation of production behavior.
- Deferred ideas stay in docs until they have real behavior and tests.

This boundary should remain protected during every v0.9.1 and future change.

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

Security has not had an external audit. The design follows modern authenticated
encryption and WireGuard-like packet posture where appropriate, but v0.9 should be
presented honestly as unaudited software for personal/lab and small-site use.

## Near-Term Priority

1. Rerun the v0.9.1 local gate listed in `docs/releases/v0.9.1.md` from a clean
   tree.
2. Refresh local package metadata and build/doctor-check v0.9.1 artifacts.
3. Retain the three-VM direct/proxied carrier evidence referenced by the release
   notes.
4. Recheck release hygiene: secrets, private paths, README, `SECURITY.md`, and
   release notes.
5. Tag `v0.9.1` only from a clean source/docs-aligned tree.

## Final Assessment

Gatherlink is functionally at v0.9.1 release-candidate quality for its declared
scope. The current tree backs the main v0.9.1 claims with code, tests, local
carrier smokes, artifact tooling, and recorded VM/proxy carrier evidence, but
the release should not be tagged until the full local gate, artifact
build/doctor check, metadata refresh, and final hygiene pass have been rerun
against the current HEAD.
