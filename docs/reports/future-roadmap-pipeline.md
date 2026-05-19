# Future Roadmap Pipeline

This is not a v1.2 roadmap.

It is a holding area for well-shaped post-v1.1 work: things that may become
v1.2, v1.3, or later after v1.1 has real usage, VM results, and soak data.
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
- No `route_id`.
- Deferred work stays docs-only until it has real behavior and tests.

## Carrier Pipeline

### CONNECT-UDP / MASQUE-Style Carrier

Why it is interesting:

- standards-based UDP proxying through HTTP infrastructure
- possible fit for environments that already support MASQUE-style proxying
- may improve deployment through HTTP-aware edge infrastructure without making
  Gatherlink itself an HTTP application

Boundary:

- future candidate, likely v1.2 or later
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
- no `route_id`

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

## Operations Pipeline

### Local REST API Hardening

Why it is interesting:

- v1/v1.1 CLI-first operation can prepare for local UI and automation

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

### Packaging And Update Channels

Why it is interesting:

- personal/lab users need boring install/update/rollback paths
- v1.1 should provide GitHub release artifacts, but future releases may need
  real channels and rollback machinery
- v1.1 should automate publishing/preparing short user docs for the GitHub Wiki
  as part of the package/release workflow

Boundary:

- Debian remains the only compatibility layer until another platform is
  explicitly promoted
- update tooling must preserve configs and secrets
- rollback must be documented before automatic updates
- v1.1 GitHub release packaging is the baseline, not the final update system
- repository docs remain canonical even when copied to the GitHub Wiki
- future improvements may harden the Wiki publishing automation, but v1.1 must
  already avoid manual-only publishing

Promotion requirements:

- package repository/channel decision beyond GitHub release artifacts
- signing/provenance decision for package channels
- service unit lifecycle tests
- upgrade/rollback VM tests
- rollback implementation that preserves configs, secrets, and state audit
- operator docs for manual and automatic recovery
- stronger automation for publishing version-matched user docs to the GitHub
  Wiki, including drift checks and rollback of bad Wiki updates

## Platform Pipeline

### Additional Linux Compatibility Layers

Why it is interesting:

- Gatherlink should be easy to port without smearing OS-specific behavior
  through the codebase

Boundary:

- Debian remains the only v1/v1.1 supported layer
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
- automatic NAT/firewall/router management
- browser-product positioning
- enterprise policy platform
- generic VPN replacement marketing

## Review Cadence

Revisit this file after:

- v1 VM acceptance
- v1 operator soak
- v1.1 carrier implementation
- v1.1 VM regression runs
- any real user report that exposes a missing operational path

When promoting an item, copy only the chosen, narrowed slice into the next
release roadmap and leave the rest here.
