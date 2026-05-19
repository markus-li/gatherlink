# Future Roadmap Pipeline

This is not a v0.9.2 roadmap.

It is a holding area for well-shaped post-v0.9.1 work: things that may become
v0.9.2, v0.9.3, or later after v0.9.1 has real usage, VM results, and soak data.
Nothing here is implementation authorization by itself.

## Promotion Rules

A pipeline item can be promoted into a real release roadmap only when it has:

- a clear user/operator problem
- a narrow owner boundary
- config shape and diagnostics shape
- unit tests and at least one realistic lab or VM acceptance path
- a migration story from existing configs
- no conflict with the project laws

Project laws still apply:

- Rust executes compact facts; Python owns meaning and lifecycle.
- Helpers never become core.
- Carriers transport the same Gatherlink UDP-format carrier packet.
- No plaintext routing.
- Routing uses authenticated session/control context and relay-hop state.
- Deferred work stays docs-only until it has real behavior and tests.

## Source TODO Parking Map

Searchable implementation TODOs may stay in source when they are tied to a
roadmap home. Current parking:

- `scheduler-telemetry`, `adaptive-scheduler`, `scheduler-hot-reapply`,
  `queue-stats`, and `rust-stats`: scheduler telemetry and fairness hardening
  in this future pipeline.
- `path-interface-discovery`, `path-transport-discovery`, and `dataplane-mtu`:
  future path/carrier discovery and MTU hardening.
- `service-id-registry`, `service-scheduler-policy`, `service-return-policy`,
  and `shared-sink-provisioning`: v0.9 production discovery/shared-sink closure
  first, then future config/provisioning polish if needed.
- `config-schema-migration`: future config compatibility work once persisted
  appliance configs exist.
- `dns-helper` and `dns-policy`: v0.9.1 DNS helper deepening or future DNS
  advanced modes, depending on scope.
- `helper-diagnostics` and `logging-diagnostics`: v0.9.1 diagnostics polish and
  future metrics/event integrations.
- `time-quality`: future time quality and authenticated time sources.
- `status-http-helper` and `status-http-write-api`: v0.9.1 REST write decision or
  future local REST API hardening.
- `cleanup-scope`: v0.9.1 operator-safe lab bundles and guided cleanup.

## Control And Reserved-Service Pipeline

These are potential future protocol lanes, not v0.9 or v0.9.1 commitments unless a
release roadmap promotes them later.

### In-Band Auth/Crypto Lane

Why it is interesting:

- could replace manual CLI/file exchange for peers that can already reach each
  other
- could support live rekey and receiver-index rotation with fewer operator
  steps
- keeps public receive behavior WireGuard-like if invalid inputs still get no
  response

Boundary:

- reserved service id `7` is not active in current code
- Python owns identity, trust roots, topology checks, transcript validation,
  rekey policy, and diagnostics
- Rust may execute only compact authenticated session facts after Python
  accepts the handshake
- invalid or unauthenticated handshake inputs must silent-drop on the network
  and produce local rate-limited diagnostics only

Promotion requirements:

- packet and transcript format with golden vectors
- replay/retry/cookie behavior and DoS bounds
- unit tests for tamper, expiry, wrong peer, stale topology, and downgrade
  attempts
- VM proof that in-band setup produces the same runtime session behavior as the
  current out-of-band Noise flow

### Dedicated Internal Service Lanes

Why they are interesting:

- some control-plane functions may eventually deserve their own flow-control,
  cadence, and decoder lifecycle instead of riding inside generic control
  metadata
- diagnostics, config apply, internal DNS, path discovery, and time sync could
  grow independently if real operator needs appear

Boundary:

- current code should use the implemented paths: control metadata id `1`,
  configured user services for helper traffic, local IPC for monitor cadence,
  scheduler hot reapply, and restart fallback
- do not add empty placeholder modules or production-looking stubs for inactive
  lanes
- each promoted lane needs production runner wiring before lab support

Promotion requirements:

- clear reason the generic control metaband or configured user service is not
  enough
- bounded message shape, decoder ownership, diagnostics, and tests
- lab/VM proof through production-owned runtime modules

## Carrier Pipeline

### Rust-Native Carrier Performance Review

Why it is interesting:

- v0.9.1 deliberately implements QUIC DATAGRAM and HTTP/3 DATAGRAM as
  Python-owned adapters around local Rust UDP path sockets
- this is clean and maintainable because it uses standard protocol libraries and
  keeps Rust focused on compact Gatherlink packet execution
- sustained high-throughput or high-packet-rate deployments may later show that
  the adapter boundary costs too much context switching or copying

Boundary:

- future optimization only; not a semantic redesign
- must carry the exact same opaque Gatherlink UDP-format carrier packets
- must not move config, helper behavior, routing meaning, carrier selection,
  diagnostics interpretation, or operator policy into Rust
- must not create a second behavior path that drifts from the Python-owned
  carrier contract

Promotion requirements:

- VM/lab benchmark showing the Python adapter is a real bottleneck, not just a
  theoretical concern
- packet-per-second and throughput comparisons for UDP, QUIC DATAGRAM, and
  HTTP/3 DATAGRAM under clean, shaped, and lossy paths
- profiling that identifies copy/context-switch overhead or protocol-library
  overhead as the limiting factor
- a small Rust DTO contract that still receives compiled carrier facts from
  Python and preserves fail-closed behavior
- parity tests proving byte identity, diagnostics, lifecycle, and failure
  behavior match the Python adapter
- rollback path to the Python adapter while the Rust-native carrier matures

### CONNECT-UDP / MASQUE-Style Carrier

Why it is interesting:

- standards-based UDP proxying through HTTP infrastructure
- possible fit for environments that already support MASQUE-style proxying
- may improve deployment through HTTP-aware edge infrastructure without making
  Gatherlink itself an HTTP application

Boundary:

- future candidate, likely v0.9.2 or later
- wrapper only for the same Gatherlink UDP-format carrier packet
- must not replace direct QUIC DATAGRAM or HTTP/3 DATAGRAM
- must not create HTTP-owned routing, identity, helper, control, encryption, or
  packet semantics
- ordinary HTTP reverse proxying is not enough

Promotion requirements:

- library/runtime choice with maintained implementation
- direct lab proving UDP packet movement through CONNECT-UDP/MASQUE
- diagnostics for tunnel establishment, datagram negotiation, target selection,
  failures, drops, and close
- fail-closed behavior when proxy support or authentication is missing
- comparison against direct QUIC DATAGRAM and HTTP/3 DATAGRAM

### WSS/TLS Fallback Carrier

Why it is interesting:

- can work through networks where UDP is blocked
- may be useful as a last-resort personal/lab escape path

Trade-offs:

- stream transport can introduce head-of-line blocking
- message framing and backpressure become more important
- it is easier to accidentally make Gatherlink look like a web app with hidden
  semantics

Boundary:

- fallback only, not the preferred carrier
- wrapper only for the same Gatherlink UDP-format carrier packet
- must preserve packet boundaries explicitly inside the stream
- must expose diagnostics that explain latency, queueing, and fallback state

Promotion requirements:

- clear MTU/message framing rules
- overload and queue bounds
- VM/lab proof under loss and blocked-UDP conditions
- explicit operator warning when using stream fallback

### TCP/TLS Carrier

Why it is interesting:

- even simpler than WSS in some environments
- useful when a small site has only basic TCP forwarding available

Boundary:

- fallback only
- no routing semantics in TLS/SNI
- wrapper only for Gatherlink UDP-format carrier packets
- must not become the baseline performance path

Promotion requirements:

- explicit packet framing
- head-of-line blocking diagnostics
- queue and memory limits
- comparison against UDP and QUIC carriers

### Obfuscation Profiles

Why it is interesting:

- may reduce easy fingerprinting of exposed carriers
- could help personal/lab deployments in restrictive environments

Boundary:

- not a promise of censorship resistance
- not allowed to weaken authentication, replay protection, or silent-drop
  behavior
- no public debug/version fingerprints
- no new Gatherlink packet model

Promotion requirements:

- threat model for each profile
- packet capture comparison tests
- CPU and overhead measurements
- clear operator warnings about limits

## Relay And Topology Pipeline

### Multi-Hop Relay Policy Automation

Why it is interesting:

- untrusted peers can forward encrypted relay sessions without seeing payload or
  plaintext routing labels
- small sites may want hub-and-spoke, chained, or backup relay paths

Boundary:

- relay policy belongs to Python
- Rust executes compact next-hop facts only
- relays must not blindly forward invalid packets
- no plaintext routing
- no plaintext routing labels

Promotion requirements:

- VM lab with at least three Debian nodes
- invalid packet rejection without decrypting endpoint payload
- relay diagnostics for hop selection, rejection, reseal, and next-hop send
- loop prevention and bounded forwarding state

### Peer Failover And Multi-Sink Services

Why it is interesting:

- a service may need more than one destination process or sink
- small sites may want backup exits without full mesh behavior

Boundary:

- service-to-peer mappings remain authenticated control/topology state
- no hidden dynamic mesh
- no endpoint IP/port mutation through control context
- failover should be explicit and diagnosable

Promotion requirements:

- config shape for primary/backup or multiple sinks
- deterministic failover policy
- duplicate/dedupe behavior documented per session/path
- VM proof with sink failure and recovery

### Topology Distribution Helper

Why it is interesting:

- today topology/provisioning can be assisted out of band
- future tooling could side-load or distribute signed bundles more comfortably

Boundary:

- signed artifacts remain canonical
- helper never becomes an enrollment authority
- no secret key leakage
- operator can inspect bundles before install

Promotion requirements:

- threat model for distribution
- rollback and revocation workflow
- trust-root UX tests
- redaction tests

### Trust-Root Lifecycle UX Hardening

Why it is interesting:

- small-site users need to understand which identities and topology bundles they
  trust without reading raw key material
- trust-root rotation, revocation, and assisted side-loading may become more
  important once real deployments exist

Boundary:

- potential future work only, not a v0.9.1 commitment
- signed artifacts remain canonical
- any assisted side-loading must be inspectable and must not become a hidden
  enrollment service
- secret material must stay redacted in status, diagnostics, reports, and helper
  output

Promotion requirements:

- concrete operator workflow for add, inspect, rotate, revoke, and rollback
- redaction tests
- VM or lab proof for side-loading through an explicit operator-approved path
- docs that distinguish trust-root UX from packet/session crypto

## Helper Pipeline

### Captive Portal Helper

Why it is interesting:

- useful for laptops or small-site links that occasionally land behind captive
  portals

Boundary:

- helper detects and reports portal state
- it must not bypass portals or implement browser automation by default
- core transport must not depend on it

Promotion requirements:

- detection-only first version
- diagnostics and operator guidance
- no privileged or invasive network behavior

### IPsec Helper

Why it is interesting:

- some sites already standardize around IPsec tooling

Boundary:

- orchestration helper only
- platform-specific behavior behind Debian compatibility tooling
- no core dependency

Promotion requirements:

- concrete small-site scenario
- clear ownership boundary with system IPsec tools
- testable plan/apply/status model

### Policy Advisor

Why it is interesting:

- can explain risky configs and recommend safer carrier/helper choices

Boundary:

- advisory only
- no automatic policy mutation unless explicitly requested
- must explain every recommendation from visible facts

Promotion requirements:

- stable rule set
- tests for warnings and non-warnings
- JSON output for automation

### DNS Advanced Modes

Why it is interesting:

- DNS-over-HTTPS upstream execution, local DNSSEC validation, and DNS racing may
  be useful in some deployments
- path-aware DNS upstream scoring could improve helper behavior when carriers
  differ in latency, loss, or reachability
- a persistent DNS cache may be useful if operators report a real need, but
  memory-only behavior should remain the baseline until then

Boundary:

- potential future work only, not a v0.9.1 commitment
- DNS remains a helper; Rust must not learn DNS semantics
- dnspython remains the preferred starting point unless a dated library decision
  changes that
- DNSSEC validation and DoH must fail closed and report clear diagnostics
- persistent caching must not leak private query history by default

Promotion requirements:

- threat model for DoH, DNSSEC, cache persistence, and query diagnostics
- unit tests for IDNA, DNSSEC success/failure, DoH failure, and cache policy
- lab or VM proof that queries traverse the intended Gatherlink service path
- operator docs explaining privacy and validation limits

### SOCKS5 UDP Associate And Stream Multiplexing

Why it is interesting:

- SOCKS5 UDP ASSOCIATE could support applications that expect UDP proxying
- shared helper stream framing may eventually reduce duplicate logic between
  SOCKS5 and TCP forwarding helpers

Boundary:

- potential future work only, not a v0.9.1 commitment
- SOCKS stays a helper; it must not become core routing or packet policy
- TCP CONNECT remains the current supported SOCKS5 scope until this is promoted
- any multiplexing layer must stay a helper transport abstraction over
  Gatherlink UDP services

Promotion requirements:

- protocol behavior tests for UDP ASSOCIATE setup, teardown, and failure modes
- overload and queue bounds
- helper diagnostics for association lifecycle, drops, and byte counters
- lab or VM proof with a real UDP-speaking client application

### Time Quality And Authenticated Time Sources

Why it is interesting:

- Network Time Security, peer time-quality exchange, HTTPS time, NTP, and
  optional GPS facts could improve internal confidence scoring
- better time quality may help future telemetry windows, replay windows, and
  operator explanations

Boundary:

- potential future work only, not a v0.9.1 commitment
- system clock setting remains opt-in and helper-owned
- Rust consumes compact effective time facts only; Python owns quality scoring
- dataplane correctness must not depend on wall-clock sync

Promotion requirements:

- confidence model for each source type
- tests for bad source disagreement, stale source data, and fail-closed behavior
- diagnostics explaining source quality without exposing private endpoints
- explicit operator docs warning when system time should be left to NTP agents

## Operations Pipeline

### Scheduler Telemetry And Fairness Hardening

Why it is interesting:

- current scheduling already keeps the important boundary: Python decides policy and
  Rust executes compact per-path facts
- policies such as `least_queue`, `balanced`, and `adaptive` are useful, but
  they need better live queue and confidence signals before they should be
  treated as production-grade automatic optimization
- small sites may run several services over the same node pair, so scheduler
  behavior should eventually account for service priority and fairness, not only
  per-path capacity and latency

Known shortcomings:

- Rust does not yet expose enough live send-queue telemetry for Python to make
  a fully honest `least_queue` decision, such as per-path queued packets, queued
  bytes, oldest queued packet age, and recent drop/backpressure state
- service priority exists as compiled policy input, but cross-service fairness
  and starvation prevention need stronger tests and diagnostics
- adaptive scheduling needs richer smoothing, decay, confidence, and provenance
  rules before it should react aggressively to noisy measurements
- receiver-side or peer-reported path quality must be treated as authenticated
  control/context input with clear trust rules, not as raw Rust-owned policy
- operator diagnostics should explain why a path was preferred, drained, or
  avoided instead of only showing the final mode and counters

Boundary:

- Python owns scheduler meaning, telemetry interpretation, smoothing,
  confidence, service priority, and operator explanations
- Rust may expose cheap queue facts and execute compiled decisions, but must not
  grow hidden policy ownership
- scheduler feedback must not create plaintext routing labels or bypass the
  authenticated control/session model

Promotion requirements:

- Rust queue telemetry for queued packets, queued bytes, oldest queued age, and
  backpressure/drop signals
- Python policy compiler support for smoothing, decay, confidence, and explicit
  service-priority fairness
- unit tests for noisy telemetry, stale telemetry, starvation prevention, and
  deterministic fallback when metrics are missing
- lab and VM tests under latency, loss, capacity limits, queue pressure, and
  multiple competing services
- diagnostics JSON and terminal output that can explain scheduler choices in a
  way an operator can act on

### Broader Runtime Reload

Why it is interesting:

- scheduler reapply is already live, but operators may eventually want broader
  config, service, helper, and control-policy reloads without restarting a node
- reload should make small-site operations less brittle when changing one
  service or helper

Boundary:

- potential future work only, not a v0.9.1 commitment
- Python owns reload meaning, validation, rollback, and diagnostics
- Rust may receive compact updated runtime facts only after Python validates the
  change
- failed reloads must preserve the previous known-good runtime state

Promotion requirements:

- reload matrix for which fields are live-reloadable, restart-required, or
  rejected
- tests for partial failure, rollback, and diagnostics
- lab or VM proof for service/helper changes during traffic
- operator docs that make reload effects predictable

### Local REST API Hardening

Why it is interesting:

- v0.9/v0.9.1 CLI-first operation can prepare for local UI and automation

Boundary:

- localhost only unless separately designed
- write access remains experimental until explicitly promoted
- write access should expire automatically unless restarted from CLI
- CLI behavior remains canonical

Promotion requirements:

- endpoint inventory
- auth decision before any non-local exposure
- tests for read/write expiry
- docs showing CLI equivalence

### Terminal Views

Why it is interesting:

- small-site users need quick status views without a GUI

Boundary:

- read-only by default
- built from existing diagnostics/status APIs
- must not create a separate state model

Promotion requirements:

- stable JSON inputs
- no secrets in views
- useful degraded/recovery displays

### Metrics And Event Stream Integrations

Why it is interesting:

- Prometheus-style metrics, WebSocket/event-stream status, or similar adapters
  may help operators integrate Gatherlink into existing monitoring
- richer integrations are useful only if they reuse the existing diagnostics and
  status facts

Boundary:

- potential future work only, not a v0.9.1 commitment
- integrations consume structured diagnostics/status; they must not create a
  second state model
- no secret material in metrics, labels, event streams, or examples
- remote exposure requires a separate authentication and threat-model decision

Promotion requirements:

- stable metric/event naming rules
- redaction tests
- load/backpressure tests for event streams
- docs explaining local-only defaults and remote-exposure risks

### Safe Mode And Recovery Mode

Why it is interesting:

- bad config, bad update, or suspected compromise may require a mode that
  prioritizes inspection and recovery over normal service operation
- operators should have a boring way to disable risky surfaces and recover
  state

Boundary:

- potential future work only, not a v0.9.1 commitment
- safe mode should disable helpers and any future overlay behavior by default
- inspect, diagnose, stop, rollback, and state-audit commands remain available
- safe mode must not silently modify secrets, trust roots, or topology bundles

Promotion requirements:

- clear trigger and exit workflow
- tests proving helpers and mutable APIs stay disabled
- rollback and state-audit integration
- operator docs for recovery from bad config, failed update, and suspected key
  exposure

### Packaging And Update Channels

Why it is interesting:

- personal/lab users need boring install/update/rollback paths
- v0.9.1 provides GitHub release artifact preparation, but future releases may
  need real channels and rollback machinery
- v0.9.1 prepares short user docs for the GitHub Wiki as part of the
  package/release workflow; future work can turn that into a fuller publish
  pipeline

Boundary:

- Debian remains the only compatibility layer until another platform is
  explicitly promoted
- update tooling must preserve configs and secrets
- rollback must be documented before automatic updates
- v0.9.1 GitHub release packaging is the baseline, not the final update system
- repository docs remain canonical even when copied to the GitHub Wiki
- future improvements may harden the Wiki publishing automation beyond the
  v0.9.1 prepared payload

Promotion requirements:

- package repository/channel decision beyond GitHub release artifacts
- signing/provenance decision for package channels
- service unit lifecycle tests
- upgrade/rollback VM tests
- rollback implementation that preserves configs, secrets, and state audit
- operator docs for manual and automatic recovery
- stronger automation for publishing version-matched user docs to the GitHub
  Wiki, including drift checks and rollback of bad Wiki updates

## Lab And Tooling Pipeline

### Built-In Test Traffic Generator

Why it is interesting:

- a tiny built-in UDP traffic generator could make labs and user troubleshooting
  less dependent on external probe scripts
- it may simplify repeatable acceptance checks for packet movement, degradation,
  and recovery

Boundary:

- potential future work only, not a v0.9.1 commitment
- lab/tooling convenience only, not a runtime dependency
- must not become a benchmark marketing tool
- should reuse existing diagnostics and service-status facts

Promotion requirements:

- focused tests for send, receive, count, timeout, and failure behavior
- docs showing when to use it instead of external tools
- lab proof that it covers the common smoke-test paths
- no volatile generated totals in durable docs

## Platform Pipeline

### Linux Endpoint Protection Helper

Why it is interesting:

- endpoints may need a small amount of Linux kernel firewall/NAT plumbing around
  Gatherlink-owned services
- operator-safe rule installation could reduce accidental exposure when running
  helpers or endpoint handoff modes
- a narrow helper could make personal/lab deployments easier without making the
  core a firewall

Boundary:

- future candidate only; not v0.9 or v0.9.1 unless a release roadmap promotes it
- helper-owned, opt-in, and Linux-specific
- limited to explicit Gatherlink endpoint scenarios
- must not manage general host firewall policy, LAN routing, SD-WAN policy,
  segmentation, IDS/IPS, QoS, or arbitrary NAT
- must use labeled nftables/iptables chains, marks, sets, comments, or other
  explicit hook points so external firewall tools can place rules before and
  after Gatherlink-owned rules
- must detect and report conflicts instead of rewriting unknown rules

Promotion requirements:

- precise rule ownership model and cleanup behavior
- coexistence tests with pre-existing rules before and after Gatherlink labels
- diagnostics for installed, repaired, skipped, conflicting, and removed rules
- Debian VM proof that endpoint protection works and fails closed
- clear docs saying this is not a general firewall manager

### Additional Linux Compatibility Layers

Why it is interesting:

- Gatherlink should be easy to port without smearing OS-specific behavior
  through the codebase

Boundary:

- Debian remains the only v0.9/v0.9.1 supported layer
- new OS behavior must enter through compatibility modules/scripts
- no platform conditionals scattered through core logic

Promotion requirements:

- real user need
- maintained test environment
- compatibility API delta documented

## Research Parking Lot

These are interesting but should not be promoted without a very strong reason:

- broad mesh product behavior
- hosted accounts or hosted coordination
- GUI-first operations
- automatic NAT/firewall/router management outside the narrow endpoint helper
- browser-product positioning
- enterprise policy platform
- generic VPN replacement marketing

## Review Cadence

Revisit this file after:

- v0.9 VM acceptance
- v0.9 operator soak
- v0.9.1 carrier implementation
- v0.9.1 VM regression runs
- any real user report that exposes a missing operational path

When promoting an item, copy only the chosen, narrowed slice into the next
release roadmap and leave the rest here.
