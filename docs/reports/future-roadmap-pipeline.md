# Future Roadmap Pipeline

This is not an active release roadmap.

It is a holding area for well-shaped work outside the active release roadmap.
Anything promoted into a release belongs in that release's roadmap, not here.
Nothing here is implementation authorization by itself.

## Promotion Rules

A pipeline item can be promoted into a real release roadmap only when it has:

- a clear user/operator problem
- a narrow owner boundary
- config shape and diagnostics shape
- unit tests and at least one realistic lab or VM acceptance path
- a migration story from existing configs
- no conflict with the project laws

Project laws still apply. The canonical policy home is
[`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md); this pipeline should not duplicate
permanent boundaries except where an item needs a local release-promotion note.

## Source TODO Parking Map

Searchable implementation TODOs may stay in source when they name a stable
feature area and point to a roadmap or owning doc. This file should not track
whether a TODO is currently implemented; that status belongs in release roadmaps
and release notes. Use this parking map only to keep future-looking TODO names
from becoming orphaned.

Current future-looking TODO areas:

- `scheduler-telemetry`, `adaptive-scheduler`, `queue-stats`, `rust-stats`,
  and `scheduler-hot-reapply`: scheduler measurement, policy reapply, and
  broader adaptive optimization beyond the active release scope.
- `path-interface-discovery`, `path-transport-discovery`, and `dataplane-mtu`:
  automatic path/interface discovery and deeper MTU handling beyond configured
  paths.
- `service-id-registry`, `service-scheduler-policy`, `service-return-policy`,
  and `shared-sink-provisioning`: provisioning and policy polish around service
  ids, peer-scoped replies, and shared-sink operation.
- `config-schema-migration`: compatibility transforms and downgrade behavior
  when a future schema revision is introduced.
- `dns-helper`, `dns-policy`, and `dns-doh`: advanced DNS policy, bootstrap,
  validation, and transport combinations beyond the current helper behavior.
- `helper-diagnostics` and `logging-diagnostics`: broader helper/runtime event
  coverage and operator reporting integrations.
- `perf`: future performance work backed by benchmark evidence, not ad hoc
  constant changes.
- `time-quality`: future time quality and authenticated time-source work.
- `status-http-helper` and `status-http-write-api`: local API hardening and
  write-operation UX beyond the current guarded operator API.
- `traffic-split-platforms`: non-Debian firewall or policy-routing backends.
- `cleanup-scope`: broader recovery and cleanup tooling beyond safe lab
  cleanup.
- `handshake-abuse-protection`, `listen-allow-deny`, and `stealth-scan-tests`:
  security and DoS-resistance hardening.

## Public Distribution And Website Pipeline

These items are public-release infrastructure. They should be promoted into a
release roadmap only when the release needs them, with the same evidence rules
as runtime features.

### Debian Package Publishing

Status: future pipeline. Package building and local install smoke belong to the
active packaging docs once implemented; publishing through an apt repository or
other distribution channel remains future until promoted by a roadmap.

Why it is interesting:

- Gatherlink is Debian-first, and a package is the cleanest user installation
  story once the CLI and service lifecycle are stable.
- A package gives repeatable install, upgrade, rollback, and file ownership
  behavior for the Python control plane and Rust dataplane binding.

Boundary:

- packaging scripts may install files, examples, completions, and docs
- packaging scripts must not auto-enable privileged helpers, rewrite network
  state, or start tunnels without explicit operator action
- Debian remains the compatibility backend; this is not broad platform support

Promotion requirements:

- release-ready package artifact from the active packaging path
- publishing workflow for an apt repository, GitHub release asset, or both
- publishing decision: local `.deb`, apt repository, or both

### Cloudflare-Hosted Public Website

Status: future pipeline. Static website copy and deployment notes may exist in
[`docs/public/`](../public/README.md); production hosting remains future until a
roadmap promotes it.

Why it is interesting:

- The public repository needs a clear landing point once releases are public.
- A small static site can explain Gatherlink, link docs/releases, and set
  expectations without building a hosted product.

Boundary:

- static website only unless a later roadmap explicitly says otherwise
- no hosted accounts, remote management, telemetry, or control-plane behavior
- no private lab hostnames, private repository names, keys, or generated local
  benchmark artifacts

Promotion requirements:

- documented site scope and source ownership
- checked links to docs and GitHub releases
- security and benchmark caveats visible without marketing overreach
- Cloudflare deployment documented separately from core runtime behavior

## UDP Multipath Future Pipeline

Configured-path scheduling and telemetry are active product areas. The
following broader items stay future unless a later roadmap promotes them
explicitly.

### Dynamic Path Discovery And Path Manager

Why it is interesting:

- configured paths are enough for v0.9.2 optimization
- automatic discovery, addition, and removal of paths is useful later for
  appliance-like deployments and changing network environments

Boundary:

- Python owns discovery, policy, and operator explanation
- Rust only receives compiled path additions/removals and socket facts

Promotion requirements:

- config/runtime migration story
- diagnostics for discovered, rejected, added, removed, and flapping paths
- lab proof that automatic path changes do not disturb existing services

### Post-V0.9.2 Multipath Performance Follow-Up

Why it is interesting:

- v0.9.2 should capture the MPTCP/WireGuard lessons that fit Gatherlink's UDP
  scope, but some deeper performance questions need longer evidence before they
  become release commitments
- WireGuard's strongest speed advantages come from kernel placement, mature
  batching/segmentation paths, tiny hot paths, and years of edge-case tuning;
  Gatherlink should learn from those without pretending to be WireGuard
- MPTCP's deeper congestion-window coupling and reinjection behavior is useful
  to study, but ordinary Gatherlink UDP payloads must remain best-effort unless
  a future service explicitly requests reliability semantics

Future work:

- profile datagrams-per-second and syscall counts before changing scheduler or
  batching constants again
- compare AEAD backends and hardware acceleration on the same host
- decide whether any carrier send/receive work should move deeper into Rust for
  speed, based on profiling rather than architecture taste
- explore congestion fairness beyond local credits without adding TCP semantics
  to normal UDP payloads
- consider optional reliability only for reserved Gatherlink metadata or
  explicitly reliable future services, never as hidden behavior for application
  UDP
- keep improving WireGuard-over-Gatherlink measurement with one-path,
  three-path, relay, clean, shaped, lossy, and reverse-direction profiles
- develop a TCP-aware proxy helper profile where TCP streams use explicit or
  transparent proxying over Gatherlink while non-TCP traffic continues through
  WireGuard-over-Gatherlink

Promotion requirements:

- repeatable benchmark reports with CPU, Linux UDP counters, path split, drops,
  queue pressure, and clean/degraded offered rates
- proof that any extra complexity beats the current Rust hot path under VM and
  non-VM conditions
- clear docs showing what is UDP best-effort, what is Gatherlink metadata
  reliability, and what remains future-only

### Relay Performance Optimization

Status: future pipeline unless a release roadmap explicitly promotes it.

Why it is interesting:

- untrusted relay routing works and has proof coverage, but product-relevant
  WireGuard-over-Gatherlink relay performance still trails raw Gatherlink and
  direct WireGuard
- relay tuning should be measured separately from endpoint and scheduler
  overhead so relay work does not hide endpoint problems

Boundary:

- relays forward encrypted Gatherlink traffic according to authenticated relay
  session state
- relays do not decrypt endpoint payloads, inspect helper semantics, or own
  endpoint policy
- Python owns relay policy and diagnostics; Rust owns compact relay packet
  execution, counters, sockets, queues, and crypto

Promotion requirements:

- repeatable raw relay and WireGuard-over-relay benchmark rows with direct
  WireGuard and raw Gatherlink baselines
- proof that any relay hot-path optimization does not change endpoint security
  semantics
- VM acceptance showing path failure, recovery, and monitor visibility through
  the relay shape

### TCP-Aware Proxy Plus WireGuard Hybrid

Why it is interesting:

- MPTCP is strong for TCP because it is TCP-aware; a single
  WireGuard-over-Gatherlink UDP peer flow cannot expose inner TCP streams
- a Gatherlink TCP proxy helper can expose stream ids, backlog, open/close,
  reset, credit, and traffic-class hints without putting TCP semantics in Rust
- non-TCP traffic can remain inside WireGuard-over-Gatherlink, keeping VPN
  behavior, keys, interfaces, and routes with WireGuard

Future work:

- explicit TCP proxy mode using configured listen endpoints
- transparent TCP proxy mode using Debian TPROXY/nftables/policy-routing rules
  owned by the traffic-split/firewall helper
- companion exit helper that connects to the configured or preserved original
  destination
- stream-level scheduler hints so one TCP stream stays sticky while separate
  TCP streams can spread across paths
- diagnostics showing explicit proxy, transparent proxy, or WireGuard fallback
  path per flow

Boundary:

- TCP stream meaning stays in the Python helper
- firewall/TPROXY mutation stays in a narrow opt-in Debian helper
- WireGuard owns non-TCP tunnel behavior
- Rust still carries framed encrypted service payloads and compact scheduler
  facts only

Promotion requirements:

- explicit-mode tests first, without firewall interception
- transparent-mode lab/VM proof that original destinations are preserved and
  Gatherlink/control/local-management traffic cannot loop through the proxy
- benchmarks comparing TCP proxy over Gatherlink against WG-over-GL and direct
  WireGuard path-set baselines on clean, fiber+5G, Starlink+5G, and LTE-style
  profiles
- cleanup/revert tests proving only Gatherlink-labeled firewall objects are
  removed

### Rust Userspace WireGuard Backend Comparisons

Why it is interesting:

- GotaTun is Mullvad's Rust userspace WireGuard implementation and is forked
  from BoringTun
- BoringTun is Cloudflare's Rust userspace WireGuard implementation, with the
  `boringtun-cli` executable for Linux and macOS
- the upstream project provides a Linux userspace binary and library and can be
  configured through normal `wg` tooling
- these are the most interesting Rust candidates to compare against
  `wireguard-go` for Gatherlink's high-rate WireGuard-over-Gatherlink paths

Implemented comparison tooling:

- `tools/hyperv/install_gotatun_backend.sh` installs a pinned GotaTun source
  ref on the Debian VM lab nodes without adding it to Gatherlink dependencies
- `tools/hyperv/install_boringtun_backend.sh` installs a pinned
  `boringtun-cli` crate version on the Debian VM lab nodes without adding it to
  Gatherlink dependencies
- `tools/hyperv/run_wireguard_onehop_speed.sh --implementation gotatun`
- `tools/hyperv/run_wireguard_onehop_speed.sh --implementation boringtun`
  runs the same one-hop WireGuard matrix shape used for kernel WireGuard and
  `wireguard-go`
- `tools/hyperv/run_performance_matrix.sh` accepts
  `wireguard-gotatun-onehop` and `wireguard-boringtun-onehop` so Rust
  userspace rows can sit beside the existing kernel and `wireguard-go` rows

Remaining work:

- run the same WireGuard-over-Gatherlink benchmark matrix with kernel
  WireGuard, `wireguard-go`, GotaTun, and BoringTun where each backend is
  available
- compare clean high-rate one-hop, clean mixed TCP+UDP, fiber+5G, Starlink+5G,
  Starlink queue dynamics, five-Starlink correlated, and LTE-style profiles
- record CPU, datagrams per second, retransmits, UDP loss, queue pressure,
  direct userspace-WireGuard path-set baseline, WG-over-GL result, `WG Gate`,
  and `Vendor Gate`
- document Linux capability, privilege-drop, `fwmark`, `wg-quick`, and daemon
  behavior differences before presenting a Rust userspace backend as an
  operator option
- retest GotaTun when upstream lands more desktop/Linux performance work; the
  current VM one-hop evidence shows GotaTun `v0.7.0` still below
  `wireguard-go` for simultaneous TCP on this host
- keep `wireguard-go` as the primary userspace WireGuard comparison baseline
  until repeated evidence says otherwise; both tested Rust userspace clients
  were roughly around half of `wireguard-go` for simultaneous TCP in the
  2026-05-25 clean Hyper-V VM shape

Boundary:

- Gatherlink must not implement WireGuard protocol behavior itself
- GotaTun and BoringTun are optional helper/backend comparisons, not core
  dependencies
- Python helper/orchestration owns backend selection, process lifecycle,
  diagnostics, and benchmark reporting
- Rust dataplane still carries opaque WireGuard UDP service payloads and does
  not learn WireGuard packet meaning

Expected result:

- baseline expectation is similar performance to `wireguard-go`
- better or worse results must be treated as evidence to investigate, not as a
  reason to change the helper default immediately
- kernel WireGuard remains the normal production ceiling where available

Promotion requirements:

- reproducible install/build instructions for each pinned Rust userspace
  backend version
- direct Rust-backend versus `wireguard-go` tests outside Gatherlink
- Gatherlink-carried Rust-backend versus `wireguard-go` tests on the demanding
  and highest-paced profiles listed above
- no new release claim until results are repeated and docs explain backend
  limits clearly

### UDP Congestion Fairness Beyond Local Credits

Why it is interesting:

- v0.9.2 credits and pacing protect Gatherlink from overrunning its own paths
- broader fairness against other traffic may require a more explicit
  congestion-control model
- Starlink/mobile links may need path-local capacity responsiveness policies so
  Python can react quickly to queue growth or capacity collapse without making
  stable wired paths flap

Boundary:

- this must not become TCP semantics or hidden retransmission
- Python owns congestion policy; Rust executes bounded pacing/credit primitives

Promotion requirements:

- repeatable tests showing fairness against competing traffic
- no hidden reliability for UDP payloads
- operator controls and diagnostics that explain when Gatherlink self-limits
- evidence that `conservative`, `adaptive`, and `volatile` capacity
  responsiveness policies improve Starlink/mobile-style profiles without
  destabilizing clean or wired profiles

### Connection Profiling And Path Profile Inference

Why it is interesting:

- real Starlink, 5G, LTE, Wi-Fi, and consumer-router paths differ in ways that
  static lab profiles only approximate
- before relying on automatic scheduler inference, operators should be able to
  profile a real connection well enough to reproduce its important behavior in
  the lab
- future device helpers, such as a 5G modem/router helper or Starlink stats
  helper, can provide strong path hints without becoming scheduler authority

Future work:

- a Python helper that records per-path capacity over time, latency, jitter,
  latency-under-load, queue age, drops, outage/handoff events, recovery time,
  and correlated events across paths
- optional helper integrations for device-specific facts, for example
  Starlink obstruction/ping-drop/router statistics or 5G modem radio
  technology, signal quality, bearer state, and reconnect events
- a profile exporter that turns observed behavior into a lab shape usable by
  the VM/lab harness, including capacity steps, jitter bursts, queue growth,
  loss windows, and recovery ramps
- a path-profile inference layer that combines fixed config, lab metadata,
  helper hints, long-window behavioral telemetry, and weak network identity
  hints into a confidence-scored profile such as `wired_stable`,
  `cellular_like`, `starlink_like`, `satellite_like`, `volatile_wireless`, or
  `unknown`

Boundary:

- Python owns profile inference and all scheduler parameter changes
- fixed operator config wins over inference
- lab profile metadata can seed the profile for short tests
- helper hints are inputs, not authority; Python decides how much to trust them
- Rust receives only compiled scheduler primitives, not device or access-type
  meaning
- this is discovery work and must not become an active release gate until a
  release roadmap explicitly promotes it

Promotion requirements:

- long-running profiler tests on at least one stable wired, one cellular-like,
  and one Starlink-like or simulated Starlink-like path
- exported lab profile can reproduce the measured path's rough throughput,
  latency-under-load, jitter, loss, and recovery shape closely enough for
  scheduler comparisons
- diagnostics show configured profile, inferred profile, confidence, hint
  sources, active responsiveness policy, and why the profile changed
- tests prove fixed config overrides inference and that short benchmark
  profiles can inject their intended path type without waiting for long-window
  inference
- no device helper may mutate a router/modem/dish unless that behavior is a
  separate explicit helper command with its own opt-in and diagnostics

### Mature WireGuard-Inspired Hardening

Why it is interesting:

- Gatherlink can keep learning from WireGuard's operational simplicity,
  anti-abuse posture, rekey discipline, endpoint handling, and small packet
  surface
- some of that is already represented in v0.9.2 pre-work, but not all of it is
  release-scoped

Future work:

- full cookie-based handshake DoS enforcement
- more mature endpoint mobility/roaming rules
- deeper rekey timer and volume-threshold validation
- long-run edge-case testing for replay, stale sessions, and malformed traffic

Boundary:

- do not claim WireGuard compatibility
- do not turn Gatherlink into an L3 VPN product
- do not move identity, helper, DNS, or operator policy into Rust

## Security And Abuse-Resistance Pipeline

### WireGuard-Inspired Security Completion

Why it is interesting:

- [`docs/protocol/security.md`](../protocol/security.md) defines the canonical WireGuard-inspired security
  posture for Gatherlink
- several items are already aligned, but some remain future work after v0.9.2
  reserves handshake anti-DoS packet fields
- keeping the remaining items grouped here prevents them from being scattered
  across unrelated roadmaps

Future work to complete or deepen:

- full cookie-based handshake DoS enforcement using the reserved handshake
  `mac1`/`mac2` and cookie-reply packet shapes
- short rate limiting for unknown handshake/auth sources, with active/recent
  authenticated peer exemptions
- roaming-friendly authenticated endpoint update rules, if runtime peer
  endpoint mobility becomes needed
- explicit crypto-suite migration policy that preserves the "minimal choices"
  posture instead of adding user-selectable cipher sprawl
- side-channel-reviewed compression policy if compression is ever considered;
  default remains no compression before encryption
- security-surface review that keeps helper, carrier, REST, and diagnostics
  code outside the packet-authentication trusted core

Boundary:

- follow [`docs/protocol/security.md`](../protocol/security.md) as the canonical security posture
- do not change encrypted data packet headers unless a later release roadmap
  explicitly promotes a protocol revision
- do not turn WireGuard inspiration into WireGuard compatibility claims

Promotion requirements:

- tests proving silent receive still holds under malformed traffic and scans
- tests proving active/recent authenticated peers are not penalized by spoofed
  invalid packets
- golden packet tests for any promoted handshake or cookie behavior
- threat-model note for endpoint mobility, compression, or crypto migration
  before any of those are implemented

### Listen Source Allow/Deny Policy

Why it is interesting:

- exposed Gatherlink UDP and alternative transport ports should be able to drop
  unwanted sources before expensive authentication work
- small sites often know the expected peer addresses or relay addresses
- allow/deny lists make accidental public exposure less risky without changing
  packet format

Boundary:

- follow [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md) and
  [`docs/protocol/security.md`](../protocol/security.md)
- this is listener admission policy, not routing policy
- defaults should remain usable for roaming peers, but hardened deployments
  should be able to opt into strict source policy

Expected shape:

- per-listener allow and deny CIDR lists for native Gatherlink UDP listeners
- equivalent policy hooks for QUIC DATAGRAM, HTTP/3 DATAGRAM, and future carrier
  listeners
- deny rules checked before allow rules, then default policy
- local diagnostics for denied packets without network responses
- config and doctor warnings when a listener is externally reachable without
  explicit source policy

Promotion requirements:

- unit tests for IPv4, IPv6, CIDR overlap, deny-before-allow, and default policy
- VM/lab proof for native UDP and at least one alternative carrier
- redacted diagnostics that identify rule ids without leaking secrets
- docs explaining NAT/shared-IP trade-offs and roaming-peer behavior

### WireGuard-Like Handshake Abuse Protection

Why it is interesting:

- fake authenticated packets should not be able to repeatedly drive expensive
  authentication or handshake work
- unknown sources should be cheap to reject before expensive crypto or
  handshake work
- active and recently authenticated peers must not be vulnerable to source-IP
  spoofing that creates punitive bans
- the default should be WireGuard-like: silent drop, cheap authentication
  admission, cookie/proof-of-return-path under load, and short rate limits
  rather than long default source bans

Recommended initial pattern:

- use the reserved handshake anti-DoS fields defined in
  [`docs/protocol/security.md`](../protocol/security.md) for cookie-style proof-of-return-path behavior
  when exposed handshake/auth setup needs protection before a session exists
- apply short per-source handshake/auth rate limits for unknown sources
- use IPv6 prefix-aware limiting for source addresses, similar in spirit to
  WireGuard's `/64` treatment
- invalid packets from active or recently authenticated sources are silently
  dropped and counted, but do not trigger source backoff
- a successful authenticated packet clears or suppresses source penalty state
- local diagnostics are rate-limited and never create network-visible errors

Optional unknown-source backoff:

- long backoff may be useful for repeated failures from sources with no active
  or recent authentication, but it should be a secondary tool rather than the
  main default defense
- if enabled, start conservatively with `1 minute`, then `5 minutes`, then
  `15 minutes`, then cap at `1 hour`
- reaching the `1 hour` cap after four failed authentication windows is a
  reasonable first proposal for unknown sources only
- failure levels should decay after a quiet period so stale mistakes do not
  permanently poison a roaming peer

Spoofing and NAT caution:

- UDP source IPs can be spoofed, so a pure source-IP penalty can be abused to
  temporarily block a legitimate peer if the attacker can guess or observe the
  peer address
- shared NATs may put several peers behind one public address
- IPv6 privacy addresses may require careful prefix-level handling
- promotion should evaluate whether penalties key on source IP, source endpoint,
  listener, peer identity hint after cheap parsing, or a combination
- excessive invalid traffic from an active/recent source may justify local
  rate-limiting, but that is a slippery slope: it must not let spoofed traffic
  degrade a legitimate peer unless the source is far beyond normal error levels
  and the mitigation is proven safer than simply dropping invalid packets early

Boundary:

- invalid traffic still receives no network response
- follow [`docs/protocol/security.md`](../protocol/security.md) for silent-drop and authentication posture
- this is receive-side abuse protection, not peer reputation or routing policy

Config posture:

- enabled by default
- disable switch allowed for labs and troubleshooting
- disabling must emit a local warning in config validation/doctor output and at
  listener startup
- expose rate-limit and optional unknown-source backoff settings as advanced
  settings, but keep defaults conservative and WireGuard-like

Promotion requirements:

- bounded memory for penalty tables
- per-listener and global limits so spoofed floods cannot create unbounded
  source-state growth
- active-session and recent-success exemptions so source backoff cannot be used
  to block known-good peers
- tests for cookie/proof-of-return-path behavior under load, short rate limits,
  optional long-backoff cap behavior, decay, successful clear, disabled mode
  warning, and diagnostics rate limiting
- tests must preserve the existing encrypted data packet/header shape; this
  feature may use handshake packet fields only
- tests for excessive invalid traffic from an active/recent source, including
  proof that normal authenticated traffic keeps flowing
- VM/lab tests with invalid packets, spoof-like source variation, NAT-like
  shared source behavior, and legitimate recovery after penalty expiry
- performance tests proving invalid traffic is dropped before expensive crypto
  or handshake work once a source is rate-limited or penalized

### Stealth And Scan Testing

Why it is interesting:

- Gatherlink's public receive posture should remain WireGuard-like: scanners
  that do not send valid packets should learn nothing useful from the network
- source policy and authentication backoff should improve abuse resistance
  without creating visible error or timing oracles
- stealth behavior should be tested continuously, not assumed

Expected lab coverage:

- native Gatherlink UDP listener under TCP and UDP scan attempts
- QUIC DATAGRAM and HTTP/3 DATAGRAM carrier listeners under generic UDP scans
  and malformed datagram traffic
- invalid authentication floods before and after source-backoff activation
- allowlist/denylist behavior from accepted and rejected source addresses
- verification that invalid traffic gets no network response from the
  Gatherlink listener
- local diagnostics are rate-limited and do not change network-visible behavior

Promotion requirements:

- repeatable scan tooling in local lab and VM acceptance
- packet capture or equivalent evidence for "no response" behavior
- timing checks where practical, especially before/after backoff activation
- docs explaining what "stealth" means operationally and what it does not
  promise

## Control And Reserved-Service Pipeline

These are potential future protocol lanes, not active-release commitments unless
a release roadmap promotes them later.

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
- follow [`docs/protocol/security.md`](../protocol/security.md) for handshake, identity, and silent-drop
  posture
- this item is about an in-band setup lane, not changing the authenticated
  session/runtime split

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
- follow the carrier and ownership contracts in
  [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md)
- compare against the existing Python-owned carrier adapter before promoting a
  Rust-native path

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

- future candidate after v0.9.2 unless a later roadmap promotes it
- follow the carrier contract in [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md)
- this is an additional carrier candidate, not a replacement for direct QUIC
  DATAGRAM or HTTP/3 DATAGRAM
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
- follow the carrier contract in [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md)
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
- follow the carrier contract in [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md)
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

- follow [`docs/protocol/relay-session-lifecycle.md`](../protocol/relay-session-lifecycle.md),
  [`docs/protocol/relay-trust-model.md`](../protocol/relay-trust-model.md), and
  [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md)
- this item is about automating policy selection and diagnostics, not changing
  the relay trust model

Promotion requirements:

- VM lab with at least three Debian nodes
- invalid packet rejection without decrypting endpoint payload
- relay diagnostics for hop selection, rejection, hop unwrap, and next-hop send
- loop prevention and bounded forwarding state

### Peer Failover And Multi-Sink Services

Why it is interesting:

- a service may need more than one destination process or sink
- small sites may want backup exits without full mesh behavior

Boundary:

- service-to-peer mappings remain authenticated control/topology state
- follow [`docs/protocol/control-context.md`](../protocol/control-context.md) for what control context may change
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
- follow [`docs/protocol/security.md`](../protocol/security.md) and [`docs/future/identity-and-topology.md`](../future/identity-and-topology.md)
- this item is about assisted distribution UX, not changing trust authority

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

- potential future work only, not an active release commitment
- follow [`docs/protocol/security.md`](../protocol/security.md) and [`docs/future/identity-and-topology.md`](../future/identity-and-topology.md)
- this item is about operator UX around trust roots, not packet/session crypto

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
- follow helper boundaries in [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md) and
  [`docs/helpers/captive-portal-helper.md`](../helpers/captive-portal-helper.md)

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
- follow helper boundaries in [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md) and
  [`docs/helpers/ipsec-helper.md`](../helpers/ipsec-helper.md)

Promotion requirements:

- concrete small-site scenario
- clear ownership boundary with system IPsec tools
- testable plan/apply/status model

### Policy Advisor

Why it is interesting:

- can explain risky configs and recommend safer carrier/helper choices

Boundary:

- advisory only
- follow helper boundaries in [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md) and
  [`docs/helpers/policy-advisor.md`](../helpers/policy-advisor.md)

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

- potential future work only, not an active release commitment
- follow [`docs/helpers/dns-helper.md`](../helpers/dns-helper.md) for DNS helper scope and
  [`docs/operations/library-selection.md`](../operations/library-selection.md) for dependency decisions
- future work should add behavior only with clear privacy, validation, and
  diagnostics tests

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

- potential future work only, not an active release commitment
- follow [`docs/helpers/socks5-helper.md`](../helpers/socks5-helper.md) for SOCKS helper scope
- TCP CONNECT remains the current supported scope until this is promoted
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

- potential future work only, not an active release commitment
- follow [`docs/helpers/time-sync.md`](../helpers/time-sync.md) for time-helper scope
- future work should improve confidence scoring without making dataplane
  correctness depend on wall-clock sync

Promotion requirements:

- confidence model for each source type
- tests for bad source disagreement, stale source data, and fail-closed behavior
- diagnostics explaining source quality without exposing private endpoints
- explicit operator docs warning when system time should be left to NTP agents

## Operations Pipeline

### Scheduler Optimization After V0.9.2

Why it is interesting:

- v0.9.2 promotes the narrow live queue telemetry, fairness, and diagnostics
  slice into the active roadmap
- once that is real and tested, later releases may use the same facts for more
  adaptive policy behavior
- small sites may eventually want richer optimization than v0.9.2 should carry

Future-only directions after v0.9.2:

- richer adaptive policy modes
- long-term per-service fairness tuning across many services
- receiver-side or peer-reported path-quality models
- operator policy presets for conservative, balanced, and aggressive behavior
- historical telemetry persistence for trend-aware decisions

Non-goals for this future item:

- re-documenting the v0.9.2 queue telemetry and fairness work
- treating `least_queue` as production queue-aware before v0.9.2 lands live
  telemetry
- adding plaintext routing labels or hidden Rust policy ownership

Boundary:

- follow [`docs/runtime/scheduler.md`](../runtime/scheduler.md),
  [`docs/protocol/control-context.md`](../protocol/control-context.md), and
  [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md)
- this item is about richer optimization after v0.9.2 proves the telemetry

Promotion requirements:

- v0.9.2 telemetry and fairness work completed and soaked
- evidence that a richer policy solves a real operator problem
- tests for noisy telemetry, stale telemetry, bad peer-reported data, and
  deterministic fallback
- lab and VM tests proving better behavior than the v0.9.2 baseline under the
  same shaped conditions
- diagnostics that explain richer decisions without exposing secret or routing
  state

### Broader Runtime Reload

Why it is interesting:

- scheduler reapply is already live, but operators may eventually want broader
  config, service, helper, and control-policy reloads without restarting a node
- reload should make small-site operations less brittle when changing one
  service or helper

Boundary:

- potential future work only, not an active release commitment
- follow [`docs/runtime/config-runtime-state.md`](../runtime/config-runtime-state.md) for reload ownership and
  rollback expectations
- this item is broader than the current scheduler reapply path

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
- CLI behavior remains canonical; see [`docs/architecture/api-surface.md`](../architecture/api-surface.md)

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
- follow [`docs/operations/diagnostics.md`](../operations/diagnostics.md) and
  [`docs/architecture/api-surface.md`](../architecture/api-surface.md)

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

- potential future work only, not an active release commitment
- follow [`docs/operations/diagnostics.md`](../operations/diagnostics.md) for diagnostics/state ownership and
  [`docs/protocol/security.md`](../protocol/security.md) for redaction posture
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

- potential future work only, not an active release commitment
- follow [`docs/runtime/state-persistence.md`](../runtime/state-persistence.md) and [`docs/protocol/security.md`](../protocol/security.md)
- safe mode should prioritize inspection and recovery over normal service
  operation

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
- follow [`docs/operations/appliance-update-strategy.md`](../operations/appliance-update-strategy.md),
  [`docs/runtime/state-persistence.md`](../runtime/state-persistence.md), and
  [`docs/operations/user-documentation.md`](../operations/user-documentation.md)
- v0.9.1 GitHub release packaging is the baseline, not the final update system
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

- potential future work only, not an active release commitment
- lab/tooling convenience only, not a runtime dependency
- follow [`docs/operations/testing-strategy.md`](../operations/testing-strategy.md) and
  [`docs/operations/diagnostics.md`](../operations/diagnostics.md)

Promotion requirements:

- focused tests for send, receive, count, timeout, and failure behavior
- docs showing when to use it instead of external tools
- lab proof that it covers the common smoke-test paths
- follow the volatile-fact rules in
  [`docs/operations/documentation-maintenance.md`](../operations/documentation-maintenance.md)

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

- future candidate only unless a release roadmap promotes it
- helper-owned, opt-in, and Linux-specific
- limited to explicit Gatherlink endpoint scenarios
- follow the firewall/NAT/helper boundary in
  [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md)
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

- Debian remains the only currently supported layer
- new OS behavior must enter through compatibility modules/scripts
- follow [`docs/architecture/architecture-contract.md`](../architecture/architecture-contract.md) for compatibility-layer
  boundaries

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
