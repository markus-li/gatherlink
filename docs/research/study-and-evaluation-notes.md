# Study And Evaluation Notes

## Purpose

This document lists adjacent projects and architectures worth studying for
Gatherlink. It is intentionally written for a public GitHub repository.

The goal is not to criticize other projects. The goal is to identify:

- what is worth learning
- what should be evaluated
- what should not be copied because it does not fit Gatherlink
- what to add to the future TODO list

Gatherlink's core remains:

```text
local UDP service -> carrier-aware multipath fabric -> remote UDP emit
```

Everything else is helper, orchestration, diagnostics, or future control-plane
work.

## Evaluation rule

For every adjacent project, evaluate:

1. What does it solve well?
2. Which specific design idea is useful for Gatherlink?
3. Which part does not fit Gatherlink's boundary?
4. What operational lesson should be captured?
5. What should be added as TODO/evaluation work?

"Does not fit" means the architecture goal is different. It is not a judgement
about the other project.

## Tailscale and Headscale

### Learn

Tailscale is useful to study for:

- DERP-style relay fallback
- MagicDNS-style naming
- subnet router UX
- node enrollment
- identity lifecycle
- route advertisement UX
- self-hosted control-plane contrast through Headscale
- mobile/client operational polish
- admin/debug UX

### Gatherlink equivalent concepts

```text
DERP-like concept      -> relay fabric
MagicDNS-like concept  -> overlay naming + DNS helper
Subnet router concept  -> site gateway / per-prefix reachability
ACL concept            -> access_policy helper, but service-scoped
```

### Does not fit Gatherlink

Gatherlink should not become a device-mesh-first WireGuard product.

Gatherlink is:

```text
transport-fabric-first
service-oriented
carrier-aware
multipath-aware
helper-extensible
```

Tailscale-level SSO, device posture, full admin console, and app-store client UX
are product maturity work, not core architecture requirements.

### TODO

- Evaluate DERP behavior for relay fabric design.
- Evaluate MagicDNS behavior for overlay naming.
- Capture identity lifecycle concepts.
- Keep WireGuard helper optional.

## Nebula

### Learn

Nebula is useful for:

- lightweight overlay identity
- certificate-based node trust
- lighthouse discovery
- simple mesh mental model
- clear node roles

### Gatherlink equivalent concepts

```text
lighthouse -> bootstrap/peer discovery/relay registry
certificate identity -> node/relay/exit identity lifecycle
```

### Does not fit Gatherlink

Gatherlink core should not become a mesh overlay protocol. Overlay routing belongs
in a helper that generates explicit configs.

### TODO

- Study lighthouse discovery for bootstrap registry design.
- Add identity lifecycle docs and trust-root model.
- Keep overlay routing explicit/generated.

## ZeroTier and Yggdrasil

### Learn

Useful for:

- overlay addressing
- topology graph thinking
- self-healing path ideas
- transit behavior
- virtual network membership concepts

### Does not fit Gatherlink

Gatherlink should avoid:

- virtual L2 as a primary product direction
- fully emergent topology in the core
- hidden dynamic routing
- distributed routing protocol complexity

### TODO

- Keep graph/planner concepts in overlay helper.
- Add hop limits, authenticated relay/session identifiers, generation IDs, and stale-topology rejection.
- Avoid uncontrolled dynamic mesh behavior.

## V2Ray, Xray, and sing-box

### Learn

These are highly relevant for:

- carrier abstraction
- obfuscation/framing layers
- fallback chains
- WSS/TLS carrier behavior
- QUIC carrier behavior
- route set concepts
- outbound chaining
- traffic camouflage philosophy
- restricted-network resilience

The most important idea is:

```text
carrier abstraction + transport disguise layering
```

Gatherlink equivalent:

```text
carrier -> obfuscation/framing -> aggregation protocol
```

### Specific transport ideas

#### WSS

Use as a compatibility carrier for restrictive networks, hotel WiFi, corporate
guest networks, or CDN-fronted relay paths.

Cautions:

- TCP head-of-line blocking
- buffering
- queue age
- backpressure
- must never block other paths

#### QUIC

Use as a first-class carrier profile, especially QUIC DATAGRAM where available.

Cautions:

- do not make QUIC the entire architecture
- Gatherlink frames must stay carrier-independent

#### TLS camouflage

Learn realistic TLS behavior, ALPN, SNI, certificate behavior, and CDN behavior.

Caution:

- prefer real protocol stacks
- avoid brittle fake TLS

#### REALITY-style thinking

The useful lesson is not to clone the feature. The useful lesson is:

```text
realistic carrier behavior matters
public unauthenticated fingerprints are dangerous
```

#### Fallback chains

Gatherlink already has the intended shape:

```text
physical link -> candidate logical paths -> test/rank -> activate best N
```

### Does not fit Gatherlink

Gatherlink should not become:

- a proxy zoo
- L7 router
- app routing DSL
- country-specific profile bundle
- endless outbound rule engine
- configuration-heavy proxy framework

### TODO

- Implement carrier profiles.
- Add WSS and QUIC carriers.
- Add automatic carrier discovery and retesting.
- Keep user config minimal.
- Add diagnostics explaining why a carrier was selected.
- Keep obfuscation framed as carrier resilience.

## MPTCP

### Learn

Useful for:

- path manager concepts
- scheduler terminology
- backup path behavior
- RTT/loss-aware scheduling
- multipath failure handling

### Does not fit Gatherlink

Gatherlink should not inherit:

- kernel dependency
- TCP semantic coupling
- MPTCP/firewall/routing gymnastics
- generic TCP reconstruction in the UDP core

### TODO

- Study scheduler/path-manager concepts.
- Keep Gatherlink UDP-first and service-oriented.
- Use TCP helper via reliable carrier later, not core TCP semantics.

## QUIC

### Learn

Useful for:

- QUIC DATAGRAM behavior
- stream/datagram coexistence
- max datagram sizing
- path migration concepts
- congestion-control lessons
- TLS-like modern transport behavior

### Does not fit Gatherlink

Gatherlink should not become "QUIC VPN".

QUIC is a carrier, not the architecture.

### TODO

- Add QUIC carrier.
- Use QUIC streams for optional TCP forwarding helper.
- Keep aggregation protocol independent from QUIC.

## HAProxy and Envoy

### Learn

Useful for operational maturity:

- health checks
- rise/fall thresholds
- slow start
- warmup
- runtime reconfiguration
- admin/status APIs
- metrics
- graceful degradation
- bounded backpressure
- config validation before activation

### Gatherlink equivalent concepts

```text
backend health        -> path/carrier/peer health
slow start            -> recovered path warmup
runtime weight change -> compiled scheduler policy update
admin socket/status   -> diagnostics event bus + local API
```

### Does not fit Gatherlink

Gatherlink should not become:

- HTTP proxy
- L7 load balancer
- xDS-scale dynamic config system in v0.9

### TODO

- Add path warmup.
- Add rise/fall windows.
- Add runtime reload validation.
- Add per-path queue age metrics.
- Add JSONL/WebSocket/Prometheus diagnostics.

## BIRD and FRRouting

### Learn

Useful for:

- route preference
- route withdrawal
- route policy
- loop prevention
- generation/version thinking
- explicit next-hop modeling

### Gatherlink equivalent concepts

```text
route preference -> service priority / exit preference
next hop         -> overlay next hop / transit hop
route withdrawal -> generated route invalidation
```

### Does not fit Gatherlink

Gatherlink should not become:

- BGP/OSPF replacement
- LAN routing daemon
- firewall routing policy manager

Firewalls and routers own LAN routing. Gatherlink may expose virtual next-hops or
generated service links.

### TODO

- Add authenticated relay/session identifiers for loop prevention and diagnostics.
- Add hop limits.
- Add topology generations.
- Add stale-topology rejection.
- Keep overlay planning generated and explicit.

## OpenZiti

### Learn

Useful for service-centric thinking:

- service identity
- least-privilege access
- service discovery
- identity-bound services
- access policy as a service boundary

### Does not fit Gatherlink

Gatherlink should not become a full ZTNA/application access platform.

### TODO

- Keep service IDs explicit.
- Use access_policy for service-boundary restrictions.
- Avoid app-layer identity gateway expansion.

## SoftEther and OpenVPN

### Learn

Useful for:

- transport adaptability
- restrictive-network compatibility
- operational maturity
- config migration
- certificate/key lifecycle
- platform packaging
- compatibility burden

### Does not fit Gatherlink

Gatherlink is not a VPN implementation.

WireGuard/IPsec/SOCKS helpers may exist, but the core remains virtual UDP
transport.

### TODO

- Document migration/versioning early.
- Keep helper modes optional.
- Avoid legacy compatibility sprawl in the core.

## Peplink, Speedify, OpenMPTCProuter

### Learn

Useful for user expectations around:

- link aggregation
- failover
- metrics
- bad-link avoidance
- setup UX
- appliance expectations
- remote relay dependency

### Does not fit Gatherlink

Gatherlink should not become:

- closed black-box appliance by philosophy
- firewall/router replacement
- MPTCP-centered product

### TODO

- Keep setup easier than route/firewall/MPTCP hacks.
- Publish clear path metrics.
- Keep self-hosting credible.
- Keep appliance support boundary clean.

## Final study TODO list

Evaluate and capture lessons from:

- DERP relay behavior
- MagicDNS naming
- Nebula lighthouse discovery
- V2Ray/Xray/sing-box carrier abstraction
- QUIC DATAGRAM sizing
- MPTCP scheduler/path manager behavior
- HAProxy health checks and slow start
- Envoy observability and dynamic config caution
- BIRD/FRR loop prevention
- OpenZiti service identity
- SoftEther transport adaptability
- OpenVPN compatibility burden
- OpenMPTCProuter deployment pain
- Peplink/Speedify user expectations

## Final avoid list

Avoid:

- configuration entropy
- firewall feature creep
- proxy ecosystem sprawl
- fake protocol mimicry everywhere
- hidden dynamic mesh behavior
- full routing daemon behavior
- L7 policy expansion
- cloud-required packet decisions
- kernel/XDP complexity before userspace is exhausted
