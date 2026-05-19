# Gatherlink Architecture Contract

Gatherlink is a carrier-aware multipath UDP transport system.

## Core purpose

Gatherlink provides virtual UDP services:

    local UDP listen -> multipath carrier fabric -> remote UDP emit

It is not a firewall, not a VPN replacement, not a classic SD-WAN appliance, and
not a general proxy framework.

## Hard boundaries

Gatherlink must not become responsible for firewall policy, NAT policy, traffic
shaping, QoS, IDS/IPS, firewall-style DPI, L7 routing, or enterprise LAN orchestration.
Those belong in OPNsense, OpenWrt, UniFi, MikroTik, Fortinet, Linux routing, or other tools.

Gatherlink's job is to give those tools better WAN transport primitives.

## Project laws

These laws are the short-form rules to repeat in implementation prompts and
code review. If a local change conflicts with one of these, the change is
probably drifting and should be redesigned before it spreads.

- Rust executes compact facts; Python owns meaning.
- No plaintext routing.
- No `route_id`.
- Helpers never become core.
- Authenticated sessions are the normal secure path; static crypto is explicit
  lab/manual fallback only.
- Every development slice gets focused unit tests and relevant lab proof.
- Operator and status output comes from structured facts.
- If a field is redundant after decrypt/context, remove it.
- If a thing only exists for compatibility with a bad earlier idea, delete it.

## Platform compatibility boundary

V1 supports Debian only. The Debian backend is the only compatibility backend
that must exist for v1.

Even so, OS-specific behavior must go through compatibility interfaces instead
of leaking through runtime, helpers, diagnostics, or protocol code. Future
platforms should be addable by implementing a new backend, not by rewriting the
system.

Platform-specific behavior includes:

- service management, currently systemd on Debian
- log access, currently files and journalctl-compatible behavior
- network inspection, currently Linux tools such as `ip`, `ss`, `tc`, and
  `/sys`
- install/layout paths such as `/etc/gatherlink`, `/var/lib/gatherlink`, and
  `/var/log/gatherlink`
- Linux capabilities and privilege checks
- lab shaping, namespaces, veth setup, bridge setup, and VM network setup
- time-helper privilege handling
- interface, route, link, and carrier discovery

The recommended Python shape is:

```text
python/gatherlink/platform/
  base.py
  debian.py
  detect.py
```

Rust should stay OS-neutral except where it directly owns normal sockets or
other portable packet execution primitives.

## Core layers

    virtual UDP service
      -> aggregation protocol
      -> obfuscation/framing layer
      -> carrier
      -> logical path
      -> physical link

Receive path reverses this.

## Python owns intelligence

Python owns JSON/Pydantic config, config expansion, path manager, interface/link validation,
carrier discovery, peer failover/failback, session-aware migration, scheduler policy/scoring,
DNS helper, diagnostics, hooks, time quality, bootstrap resolution, helper orchestration, and
future overlay path planning.

Python also owns control metaband policy: which sparse telemetry/control facts
are sent, how often they are sent, and when diagnostics may temporarily raise
that cadence for observability. The lab must use the same control policy modules
as production code so tests exercise the real boundary. Service monitor is a
general diagnostics client: it may request temporary higher-rate control
metadata from any service, and the service must automatically return to baseline
cadence after the request expires.

Python owns every reserved-service payload decoder. Rust may recognize that a
compact service id is reserved so it can keep those bytes away from application
UDP targets, but it must queue the payload for Python instead of interpreting
control metadata, remote status, DNS helper messages, config apply messages, or
future auth handshakes. If Python receives a reserved id without a decoder, or a
non-reserved id through the reserved dispatcher, it logs a loud error and drops
the payload.

## Rust owns packet execution

Rust owns packet receive/send, encoded runtime state execution, frame encode/decode, AEAD
envelope validation, replay window, dedupe, tiny reorder buffer, bounded queues, cheap counters,
MTU eligibility checks, and minimal hot-path weighted scheduling.

Rust must not own business policy, config interpretation, Linux environment policy, DNS policy,
peer strategy, overlay planning, or helper behavior.

Rust also must not grow semantic control-plane branches. The allowed Rust
reserved-service behavior is deliberately mechanical: forward `0..255` payloads
to Python, send Python-provided service payloads through the Python-compiled
service/path scheduler primitives, maintain cheap counters, and execute
Python-compiled runtime state.

## Non-root design

The main Gatherlink service should run unprivileged.

No raw sockets, TUN/TAP, iptables, nftables, policy routing, or CAP_NET_ADMIN should be required
for normal operation.

Privileged behavior is isolated into narrow helpers only, such as the optional time-helper with
CAP_SYS_TIME.

Local lab setup tools may run as root to create network namespaces, veth pairs, routes, and
traffic shaping. That privilege belongs to the test environment, not to Gatherlink. The Gatherlink
processes started inside the lab must still run unprivileged.

## Physical links and logical paths

A physical link is an interface/source/gateway reality.

A logical path is a carrier plus obfuscation profile over a physical link.

One physical link may expose raw UDP, stealth UDP, QUIC datagram, WSS/TLS, and TCP/TLS fallback.

Every carrier transports the same Gatherlink UDP-format carrier packet. Direct
QUIC DATAGRAM, HTTP/3 DATAGRAM, future TCP/TLS, future WSS, and obfuscation
profiles are outer wrappers only; they do not create alternate Gatherlink packet
formats and do not change routing, encryption, replay, aggregation, or service
semantics.

HTTP/3 DATAGRAM request/session machinery belongs to the carrier only. It must
not become Gatherlink control state, routing state, identity state, helper
state, or a second packet model.

On receive, an alternative carrier unwraps its outer transport and then feeds
the recovered Gatherlink packet into the same receive path used by the UDP
carrier. Packet-time unwrap work belongs in a fast component; Python may own
configuration, lifecycle, and diagnostics, but not expensive per-packet carrier
policy.

A per-physical-link selector chooses the best N logical paths. The global scheduler then schedules
over active logical paths.

## Path validation

Each physical path must validate interface existence, link state, usable source IP, configured
source IP ownership if static, deterministic gateway/route behavior, same-subnet multi-WAN source
IP/gateway uniqueness, and startup probe success.

Invalid paths are disabled and reported, not silently used.

## Carrier discovery

Carrier discovery must support manual forced retry, periodic retest, retest on failure, retest on
link recovery, and backoff for failed profiles.

Discovery ranks candidate logical paths per physical link and activates the best configured number.

## Scheduler rule

MVP scheduler is fixed/weighted round-robin.

Adaptive scheduling must wait until receiver metrics are trustworthy.

The scheduler should consume local queue age/depth, RTT, remote receiver metrics, loss estimate,
jitter, reorder rate, carrier type, HOL/blocking risk, MTU eligibility, path state, and manual weights.

Python owns the rich policy model and compiles it into small Rust execution
primitives. Rust may follow path state, weight, capacity hints, latency hints,
loss estimate, reorder hold time, and in-flight limits, but it must not decide
how those values are derived. That keeps production scheduling explainable:
policy and smoothing stay in Python, while Rust receives deterministic
per-path values that are cheap to consult in the packet path.

## Receiver metrics

Remote metric reporting is mandatory for serious scheduling. Do not ACK every packet.

Receiver reports should include received packet count, missing sequence ranges or loss estimate,
unexpected duplicates, expected fanout duplicates suppressed before application emit, out-of-order count,
jitter, receive rate, auth/decode failures, and last received sequence.

## MTU policy

Default must be conservative.

Initial behavior: use safe default payload MTU, probe when enabled, track effective payload MTU per
logical path, skip paths that cannot carry the packet, drop only when no eligible path exists, and
count/report MTU drops loudly.

Internal fragmentation exists in the Rust packet executor for packets that do
not fit the compiled path MTU or need to use available capacity on a smaller
path. Python still owns MTU policy, path state, and operator-facing warnings.

## Security

Public UDP listeners must be silent:

    invalid/auth-failing packet -> silent drop

No public version mismatch replies, debug hints, or unauthenticated errors.

Transport security uses AEAD/session envelope/replay windows.

Sealed-secret UX is only for at-rest local secrets and sealed config/provisioning
bundles, never for packet transport.

Before packet crypto is implemented, local labs may use explicit `security.mode = "none"`.
That mode is unauthenticated and unencrypted, must produce loud Python-owned warnings, and must
remain available later only for controlled labs or debugging.

## Obfuscation

Obfuscation/framing is part of the core transport boundary but remains pluggable.

It may support none, stealth UDP, random padding, QUIC-like framing, DTLS-like framing, and future
custom profiles.

Obfuscation must not leak public debug/version fingerprints.

## Time model

Monotonic clocks are used for RTT, jitter, queue age, debounce windows, and failover timers.

Wall-clock quality is used for logs, signed timestamps, expiry, diagnostics correlation,
approximate one-way latency, and config/session validity.

The main process maintains internal time quality from NTP, direct NTP, tunnel NTP, peer exchange,
HTTPS sanity checks, and optional GPS.

If system time should be changed, the main process sends a correction request to the separate
privileged time-helper. The main process must not require CAP_SYS_TIME.

## Bootstrap

Bootstrap resolution must avoid tunnel dependency.

Bootstrap is not just DNS lookup. It is resolve candidate endpoint -> try path/carrier/profile ->
authenticated probe -> cache only after success.

Methods may include cache, static IP, direct DNS, DoH, and later HTTPS metadata.

The current implementation supports static endpoints, last-known cache entries,
an explicit insecure lab probe, and a signed bootstrap challenge proof for
authenticated endpoint validation. It must not treat the plaintext lab probe as
production authentication. Python may promote endpoints into the cache only after
the authenticated proof validates the peer identity and endpoint. Rust should
still only receive the compiled path/service runtime state that results from
that decision.

## DNS helper

The DNS helper is a connectivity helper, not a firewall DNS replacement.

It exposes a normal upstream resolver for tools like AdGuard Home, Unbound, dnsmasq, OPNsense, or UniFi.

It may support path-aware DNS, tunnel/direct/DoH racing, first valid response wins, DNSSEC validation,
serve-stale cache, per-domain strategies, and external domain-set files.

Core transport must not depend on DNS helper.

## Peer failover

Peer failover is v1, not MVP.

It must support peer priority, automatic failover, conservative failback, session-aware partial failback,
minimum dwell windows, standby peer probing, new sessions using recovered preferred peer, and existing
sessions migrating only when idle or after configured force window.

## Overlay routing helper

Gatherlink core does not implement mesh routing.

A future overlay-routing helper may coordinate multi-node or multihop Gatherlink links by selecting
and provisioning explicit point-to-point services between nodes.

It may support overlay topology graph, relay-chain selection, final exit-node selection,
site-to-site gateway selection, per-prefix reachability metadata, allowed transit use, health-aware
path planning, and generated explicit node configs.

A node does not have one fixed global role. Roles are assigned per service or overlay path. The same
node may be an entry, relay, exit, or site gateway depending on the service being carried.

Gatherlink may expose a virtual next-hop/gateway for a firewall to use for a specific prefix or
destination class. The firewall/router still owns LAN policy, NAT, ACLs, and segmentation.

## Diagnostics

Diagnostics are an event bus, not a WebSocket feature.

Sinks may include local WebSocket, stdout, JSONL file, Prometheus, MQTT, and webhooks later.

Diagnostics must never block dataplane or path-manager loops.

Rust emits structured state, counters, and execution events. Python owns terminal display, logs,
warning wording, event sinks, and mapping raw events back to operator-facing explanations.

## Hooks

Hooks are external reactions to normalized events.

They may restart modems, cycle PoE, call APIs, notify Node-RED/Home Assistant, or collect diagnostics.

Hooks must have timeout, debounce, rate limit, and failure isolation.

Hooks must not become part of the scheduler.

## Helpers

Helpers are optional integrations.

Current helper priority and deferral decisions live in `docs/helpers/helper-priorities.md`.
Do not maintain a competing helper roadmap here.

Helpers may provide metadata/intent. They must not define core architecture.

## Deployment model

Gatherlink should work beside existing firewalls.

The appliance may present logical WANs via VLANs or routed handoff.

Running OPNsense/OpenWrt/etc. in a VM on the appliance may be possible for power users, but it is not
part of the official product contract.

## Open-source boundary

The useful intelligence should remain open source: engine, CLI, path manager, auto carrier discovery,
DNS helper, diagnostics, lab tooling, and overlay helper if/when implemented.

Commercial value may live in hardware appliance, hosted relays, managed UI, fleet management, updates,
monitoring, and support.

## File-specific TODO

- Keep this document aligned with actual implementation decisions.
- Do not allow new helpers or features to violate the core boundary.
- Update this before accepting major architectural pull requests.
## Captive portal helper

Captive portal support is a helper, not part of the core transport.

The canonical primitive is a temporary SOCKS5 proxy pinned to the captive WAN.
Different UX modes may sit on top:

- manual/PAC browser configuration
- streamed browser session
- standalone login browser/app
- appliance/custom Chromium profile

The helper must not rely on HTML rewriting, HTTPS MITM, DNS interception, or
transparent proxying as the primary design.

The helper must be temporary, local/LAN scoped, explicitly activated, pinned to
one WAN, and shut down after success, failure, or timeout.

## Policy advisor

A future policy advisor may tune scheduler parameters from metrics/history.

It is not the scheduler and must not replace deterministic packet selection.

Correct model:

    metrics/history -> advisor -> scheduler parameters -> deterministic scheduler

The advisor should run locally by default and use lightweight statistical or ML
techniques, not LLM-based hot-path routing.

## IPsec helper

Gatherlink may provide templates/helpers for IPsec NAT-T:

- UDP/500
- UDP/4500

Gatherlink does not directly support raw ESP protocol 50 or AH protocol 51 in
the core, because that would turn the project into a generic IP tunnel/firewall
style system.
## Comprehensive helper and study update

This repository includes public-facing study notes and full helper design notes
for adjacent architectures and future Gatherlink helper layers.

The important additions are:

- relay fabric
- overlay routing
- transit/multihop forwarding
- overlay naming
- access policy
- service priority
- captive portal helper centered on temporary SOCKS5
- policy advisor
- IPsec NAT-T helper
- identity/topology lifecycle
- loop prevention
- deployment archetypes

These are helper/control-plane areas. They must not turn the core dataplane into
a firewall, routing daemon, L7 proxy, VPN stack, or hidden dynamic mesh.
