# Gatherlink Documentation

Start here when you need to find the right design note quickly. The docs are
split into canonical implementation guidance, helper specs, future design notes,
and historical reports.

## Fast Paths

| If you are working on... | Start with | Then read |
| --- | --- | --- |
| Using Gatherlink | `docs/user/README.md` | `docs/user/troubleshooting.md` |
| Common config shapes | `docs/user/config-cookbook.md` | `docs/runtime/configuration.md`, `docs/runtime/config-runtime-state.md` |
| Operating a v0.9 node | `docs/operations/v0.9-operator-runbook.md` | `docs/operations/v0.9-troubleshooting-guide.md`, `docs/operations/diagnostics-dictionary.md` |
| Packet layout or frame overhead | `docs/protocol/protocol.md` | `docs/protocol/protocol-notes.md`, `docs/protocol/security.md` |
| Crypto, replay, identity, or stealth receive | `docs/protocol/security.md` | `docs/future/identity-and-topology.md`, `docs/protocol/relay-trust-model.md` |
| Relay forwarding | `docs/protocol/relay-session-lifecycle.md` | `docs/helpers/relay-fabric.md`, `docs/protocol/relay-trust-model.md`, `docs/protocol/protocol.md` |
| Runtime sessions, services, and paths | `docs/protocol/runtime-session-model.md` | `docs/protocol/control-context.md`, `docs/runtime/scheduler.md` |
| Config loading or runtime state | `docs/runtime/config-runtime-state.md` | `docs/runtime/configuration.md`, `docs/architecture/architecture-contract.md` |
| Helper scope and priority | `docs/helpers/helper-priorities.md` | the specific helper doc |
| Dependency choice | `docs/operations/library-selection.md` | the feature/helper doc needing the dependency |
| Diagnostics or operator status | `docs/operations/diagnostics-dictionary.md` | `docs/operations/diagnostics-events.md`, `docs/operations/diagnostics.md`, `docs/architecture/api-surface.md` |
| Failure behavior | `docs/runtime/failure-model.md` | `docs/runtime/resource-guardrails.md`, `docs/runtime/state-persistence.md` |
| Tests to write | `docs/operations/testing-strategy.md` | the relevant feature doc |
| Labs and demos | `docs/labs/local-dual-path-lab.md` | `docs/labs/wsl-two-distro-lab.md`, `docs/labs/real-vm-acceptance.md`, `docs/labs/lab-demo.md`, `docs/protocol/plaintext-security-mode.md` |
| Where code lives | `docs/architecture/source-map.md` | `docs/architecture/architecture-contract.md` |
| Future design notes | `docs/future/README.md` | `docs/reports/v0.9.1-roadmap.md`, `docs/reports/future-roadmap-pipeline.md` |
| Releasing v0.9 | `docs/operations/v0.9-release-checklist.md` | `docs/operations/release-development-process.md`, `docs/reports/v0.9-roadmap.md`, `docs/labs/real-vm-acceptance.md` |
| Release notes | `docs/releases/v0.9.0.md` | `docs/project-living-assessment.md`, `docs/operations/v0.9-release-checklist.md` |
| Project status and next work | `docs/project-living-assessment.md` | `docs/reports/README.md`, `docs/reports/v0.9.1-roadmap.md`, `docs/operations/testing-strategy.md`, `docs/helpers/helper-priorities.md` |

## Canonical Docs

These docs are current implementation guidance.

## User Docs

User documentation is for people running Gatherlink, not developing it.

- `docs/user/README.md`: short entry point for normal users
- `docs/user/config-cookbook.md`: small common config shapes
- `docs/user/core-service.md`: basic UDP service run path
- `docs/user/socks5.md`: SOCKS5 helper usage
- `docs/user/wireguard.md`: WireGuard-over-Gatherlink usage
- `docs/user/troubleshooting.md`: status, logs, monitor, and bug reports

### Architecture And Boundaries

- `docs/architecture/architecture-contract.md`: ownership boundaries between Rust, Python,
  helpers, security, diagnostics, and future features
- `docs/architecture/architecture.md`: concise architecture overview
- `docs/architecture/design-principles.md`: project design principles
- `docs/architecture/performance-philosophy.md`: where performance work belongs
- `docs/architecture/source-map.md`: where code lives and which boundary owns it
- `docs/architecture/api-surface.md`: expected public/local APIs
- `docs/architecture/plugin-strategy.md`: extension/plugin stance

### Protocol And Security

- `docs/protocol/protocol.md`: packet/frame layout, v1/v2 headers, relay packet shape,
  service ids, aggregation, and fragmentation
- `docs/protocol/security.md`: crypto model, replay protection, handshake posture, identity,
  stealth receive, and secure relay forwarding
- `docs/protocol/runtime-session-model.md`: node identities, peer sessions, services, paths,
  receiver indexes, duplicates, and v1 compatibility views
- `docs/protocol/control-context.md`: authenticated runtime control state, service mappings,
  generations, helper control, and security limits
- `docs/protocol/relay-session-lifecycle.md`: encrypted relay-hop forwarding lifecycle
- `docs/protocol/relay-trust-model.md`: what relays can and cannot learn or decide
- `docs/protocol/capability-negotiation.md`: authenticated capability negotiation rules
- `docs/protocol/plaintext-security-mode.md`: explicit local lab mode before/alongside crypto
- `docs/protocol/secrets-age.md`: age use for at-rest secrets only

### Config, Runtime, And State

- `docs/runtime/config-runtime-state.md`: human config versus compiled runtime state,
  live reload, numeric id policy, and JSON introspection
- `docs/runtime/configuration.md`: current config schema and CLI examples
- `docs/runtime/state-persistence.md`: what persists, what never persists as authority, and
  storage locations
- `docs/runtime/failure-model.md`: fail-closed and degraded behavior
- `docs/runtime/resource-guardrails.md`: bounded queues, limits, and overload behavior

### Scheduling, Paths, And Networking

- `docs/runtime/scheduler.md`: path scheduling model and packet-time decisions
- `docs/runtime/path-lifecycle.md`: path state lifecycle
- `docs/runtime/nat-traversal.md`: NAT traversal posture
- `docs/runtime/ipv6-strategy.md`: IPv6 assumptions and handling
- `docs/runtime/service-priority.md`: service priority model

### Diagnostics, Testing, And Operations

- `docs/operations/diagnostics-events.md`: structured events, stable event codes, JSONL first,
  helper warnings, and operator explanations
- `docs/operations/diagnostics-dictionary.md`: operator meaning for common counters
  and event codes
- `docs/operations/diagnostics.md`: broader diagnostics guidance
- `docs/operations/v0.9-operator-runbook.md`: day-to-day v0.9 start, inspect, stop,
  and health checks
- `docs/operations/v0.9-troubleshooting-guide.md`: scenario-based v0.9 diagnosis
- `docs/operations/v0.9-release-checklist.md`: release gates before tagging
  v0.9
- `docs/operations/release-development-process.md`: reusable three-pass
  implementation, verification, and boundary-review process for release work
- `docs/operations/testing-strategy.md`: unit, integration, relay, DNS, helper, and golden-vector
  test expectations
- `docs/operations/user-documentation.md`: short user-doc style rules and
  GitHub Wiki publishing posture
- `docs/labs/wsl-two-distro-lab.md`: repeatable two-distro WSL lab with three
  loopback carrier LANs, tc shaping, managed services, and acceptance checks
- `docs/labs/real-vm-acceptance.md`: v0.9 real Debian VM acceptance target and
  simple deploy-script posture
- `docs/labs/hyperv-vm-lab.md`: host-specific Hyper-V Debian VM lab notes and
  expected private path layout
- `docs/labs/quic-traefik-proxy.md`: v0.9.1 direct QUIC DATAGRAM carrier lab
  through Traefik UDP forwarding
- `docs/labs/http3-datagram-carrier.md`: v0.9.1 HTTP/3 DATAGRAM carrier lab
- `docs/reports/README.md`: status and purpose of every report file
- `docs/reports/v0.9-roadmap.md`: v0.9 implementation order and acceptance target
- `docs/reports/v0.9-code-audit-followups.md`: closed source/docs alignment
  findings from the v0.9 audit
- `docs/reports/v0.9.1-roadmap.md`: v0.9.1 hardening and small-site operations
  roadmap after VM acceptance and soak
- `docs/reports/future-roadmap-pipeline.md`: post-v0.9.1 pipeline ideas that are
  shaped but not assigned to a release
- `docs/project-living-assessment.md`: current release-health assessment,
  release gates, risks, and near-term priority order
- `docs/operations/appliance-update-strategy.md`: appliance update and rollback posture
- `docs/operations/deployment-archetypes.md`: expected deployment shapes

## Helper Docs

`docs/helpers/README.md` is the helper index. `docs/helpers/helper-priorities.md`
is the source of truth for what should be developed now and what is deferred.

### Active Helpers

- `docs/helpers/time-sync.md`: derived time helper and optional explicit system-time setter
- `docs/helpers/dns-helper.md`: DNS helper using dnspython, DNSSEC, IDNA, policy, and cache
  behavior
- `docs/helpers/socks5-helper.md`: Python SOCKS5 TCP CONNECT helper over Gatherlink transport
- `docs/helpers/tcp-forwarding-helper.md`: simple one-to-one TCP forwarding helper
- `docs/helpers/wireguard-helper.md`: WireGuard orchestration helper using WireGuard tooling
- `docs/helpers/relay-fabric.md`: relay discovery and health helper

### Deferred Helpers And Future Helper Areas

- `docs/helpers/captive-portal-helper.md`
- `docs/helpers/ipsec-helper.md`
- `docs/helpers/policy-advisor.md`
- overlay routing, overlay naming, loop prevention, access policy, and related
  full design notes listed below

Deferred docs may preserve design notes and future interface ideas, but they do
not authorize runtime placeholder packages or implementation work unless
`docs/helpers/helper-priorities.md` marks the helper active.

## Future Design Notes

These docs are useful when planning future work. They are not automatic
implementation authorization.

- `docs/future/README.md`
- `docs/future/access-policy.md`
- `docs/helpers/captive-portal-helper.md`
- `docs/operations/deployment-archetypes.md`
- `docs/future/identity-and-topology.md`
- `docs/helpers/ipsec-helper.md`
- `docs/future/loop-prevention.md`
- `docs/future/overlay-naming.md`
- `docs/future/overlay-routing.md`
- `docs/helpers/policy-advisor.md`
- `docs/helpers/relay-fabric.md`
- `docs/runtime/service-priority.md`

Future docs are intentionally docs-only until promoted by a release roadmap.
Avoid companion `-full.md` copies; keep one canonical file per topic unless a
split has a clearer subject name.

## Reports And Historical Notes

These are reference material, not the primary spec:

- `docs/research/study-and-evaluation-notes.md`
- `docs/reports/mvp-implementation-priorities-closed.md`
- `docs/reports/three-path-scheduler-lab.md`

Use them for rationale and old comparisons. Promote still-current decisions into
canonical docs before implementing from them.

## Stale Information Rule

If two docs disagree:

1. Prefer `docs/protocol/protocol.md` for wire layout.
2. Prefer `docs/protocol/security.md` and `docs/protocol/relay-session-lifecycle.md` for secure transport
   and relay behavior.
3. Prefer `docs/protocol/runtime-session-model.md` for sessions, services, paths, and
   receiver indexes.
4. Prefer `docs/runtime/config-runtime-state.md` for config/runtime boundaries.
5. Prefer `docs/helpers/helper-priorities.md` for helper scope and priority.
6. Prefer `docs/operations/v0.9-operator-runbook.md` and
   `docs/operations/v0.9-troubleshooting-guide.md` for current operator flows.
7. Treat reports and study notes as historical unless a canonical doc cites
   them.

## Keeping Navigation Useful

When adding or changing docs:

- update this file if the new doc is a likely entry point
- keep one concise doc as the canonical implementation target
- do not add `-full.md` companion docs; expand the canonical doc or split by a
  clearer subject name
- link from feature docs to the canonical boundary doc they depend on
- avoid duplicating active protocol, crypto, config, or helper-scope decisions
- do not record volatile totals such as test counts, file counts, generated
  artifact counts, or other snapshot numbers; record commands, scope, and
  pass/fail status instead
- implementation TODOs are allowed when they are searchable and tied to a clear
  feature area, roadmap, audit follow-up, or release gate; remove them when the
  work is done instead of letting stale notes become pseudo-requirements
- keep transient operational facts out of durable docs unless they are part of
  a dated release or acceptance report: generated report paths/timestamps,
  personal machine paths, VM inventory, hostnames, keys, raw command dumps,
  dependency popularity claims, and CI/test totals should not appear in
  canonical docs
- dependency or library choice claims that may age should live in
  `docs/operations/library-selection.md` or a dated decision/report note, not as
  timeless assertions in user or protocol docs

## Writing User Documentation

User documentation must stay short, step-by-step, and scenario-based.

- write for common real uses, not every possible feature
- split usage by helper or workflow, especially SOCKS5 and WireGuard
- link to `docs/user/config-cookbook.md` for config patterns
- link to `docs/operations/v0.9-operator-runbook.md` for day-to-day operation
- keep commands copyable and examples small
- explain only what the user needs to run, check, and stop the service
- put troubleshooting near the user path: status, logs, monitor, diagnostics
- mention current platform testing plainly: Debian tested, most Linux expected
- ask users to report bugs as GitHub issues
- move implementation rationale to design docs, not user docs
