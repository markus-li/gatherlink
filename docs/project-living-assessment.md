# Project Living Assessment

Last updated: 2026-05-19

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
  v0.9.1 work, or future pipeline work

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

Gatherlink is v0.9-ready for the deliberately narrow Debian personal/lab and
small-site scope. The planned v0.9 VM release gates have passed; the remaining
work before `v0.9.0` is tag/release hygiene.

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
- managed helper slices for SOCKS5 TCP CONNECT, TCP forwarding, DNS direct and
  tunnel upstream, WireGuard planning/transport proof, and the narrow time
  helper
- local, WSL, Hyper-V VM, relay/routing VM, and soak evidence for the declared
  v0.9 scope

## V0.9 Scope

V0.9 is for Debian personal/lab users and small sites.

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

Deferred beyond v0.9:

- production GUI
- broad platform support beyond Debian
- automatic package/update/rollback channels
- alternative carriers such as QUIC, HTTP/3 DATAGRAM, WSS, or TCP/TLS
- CONNECT-UDP/MASQUE
- obfuscation profiles
- multi-hop relay policy automation beyond the implemented first slice
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

Before tagging `v0.9.0`:

- review and retain the generated VM/soak reports as release evidence
- confirm `SECURITY.md` and release notes state the real Debian-only,
  unaudited, limited-real-world-testing posture
- confirm committed files contain no private VM hostnames, addresses, inventory
  paths, keys, or operator-only material
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
- DNS helper direct and Gatherlink-tunnel upstream paths with cache, IDNA, and
  AD-bit DNSSEC handling
- WireGuard planning/config helper and endpoint/UDP transport proof
- narrow time helper

Implemented but intentionally limited:

- runtime reload currently covers scheduler reapply; broader config reload is
  future/polish
- WireGuard helper does not own interface lifecycle
- DNSSEC support is upstream-AD oriented; full local validation and DoH remain
  optional later work
- relay policy automation is a first slice, not a full mesh controller
- REST API is planned as experimental helper/service work, not stable v0.9 UX

Deferred:

- direct QUIC DATAGRAM and HTTP/3 DATAGRAM carriers are v0.9.1 targets
- packaging, GitHub release artifacts, checksums, and automated user-doc/wiki
  publication are v0.9.1 targets
- richer trust-root UX, live rekey automation, broader diagnostics polish,
  optional DNS tunnel variants, optional WireGuard lifecycle automation, and
  multi-hop relay policy automation are tracked in v0.9.1
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

1. Review and retain the generated VM/soak reports as release evidence.
2. Recheck release hygiene: secrets, private paths, README, `SECURITY.md`, and
   release notes.
3. Tag `v0.9.0` only from a clean source/docs-aligned tree.
4. Move immediately to the v0.9.1 roadmap for packaging, wiki/user-doc
   publication, QUIC/HTTP3 DATAGRAM carriers, and deferred hardening.

## Final Assessment

Gatherlink is functionally at v0.9 for its declared scope. The planned v0.9 VM
release gates have passed, including the untrusted relay/routing proof. The
remaining work is release evidence review and hygiene, not broad product design
or milestone completion.

If the final release review finds no blocking source/protocol/security-boundary
issue, this repository is ready to tag as `v0.9.0`.
