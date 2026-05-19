# DNS Helper

The DNS helper is an active helper priority.

It should expose a normal local resolver endpoint for tools such as AdGuard
Home, Unbound, dnsmasq, OPNsense, UniFi, and similar DNS frontends. It is a
connectivity helper, not a firewall DNS replacement and not a core transport
dependency.

First scope:

- local resolver endpoint
- cache and serve-stale behavior
- upstream policy that can use direct or Gatherlink-tunnel choices for v1
- DoH policy shape may exist, but DoH execution is deferred unless explicitly
  promoted later
- IDNA-aware name handling
- DNSSEC support
- diagnostics for upstream choice, cache state, and validation failures

Implemented first slice:

- `gatherlink helpers dns-serve --listen 127.0.0.1:5353 --upstream 1.1.1.1:53`
  exposes a local UDP resolver endpoint
- `--tunnel-upstream peer-dns=127.0.0.1:55153` sends DNS wire queries to a
  configured local Gatherlink UDP service endpoint; Gatherlink then carries
  that datagram to the peer's DNS exit/service target
- DNS packets are parsed and rendered with `dnspython`; Gatherlink does not
  hand-roll DNS wire parsing
- cache keys use IDNA-aware canonical names, query type, and class
- answers are cached by TTL and may be served stale for a bounded window
- direct upstreams execute now
- Gatherlink tunnel upstream execution is implemented in the helper and must be
  included in VM acceptance before a v1 tag
- DoH is represented in policy and reports as not implemented until its
  transport is wired
- DNSSEC policy is explicit: `off`, `allow_unsigned`, or `require_ad`; the
  first implementation treats upstream `AD` as validation evidence and exposes
  failures in diagnostics rather than silently degrading

Library decision:

- use `dnspython` as the first DNS helper dependency
- prefer base `dnspython` for initial resolver/packet/cache work
- include `dnspython[dnssec,idna]` when implementing the DNS helper, because
  DNSSEC support should be part of the helper and IDNA matters
- add `dnspython[doh]` only when implementing DNS-over-HTTPS
- do not hand-roll DNS packet parsing

Viability notes:

- `dnspython` is the best fit: maintained, widely used, supports DNS messages,
  resolver APIs, async APIs, EDNS, DNSSEC, and DoH extras.
- `dnslib` is a possible fallback/reference for simple DNS server examples, but
  it is not the default because its project posture is closer to maintenance
  mode.
- `aiodns`/`pycares` is useful for async client resolution, but it is not the
  main DNS helper library because the helper needs packet parsing, inspection,
  caching, response construction, and later validation.
- Python stdlib resolver calls are insufficient for a DNS helper that exposes a
  resolver endpoint.

DNSSEC posture:

- DNSSEC support should be built into the DNS helper design.
- Validation policy should be explicit and diagnostics-visible.
- Validation failures must not silently degrade into trusted answers.
- DNSSEC behavior should be testable with known-good, unsigned, and broken
  domains.

IDNA posture:

- The helper must handle internationalized names deliberately.
- Normalize and validate names consistently before cache lookup, upstream
  policy, diagnostics, and response construction.
- Diagnostics should preserve enough information for operators to understand
  the queried name without creating ambiguous cache keys.

Not-yet scope:

- DNS-over-HTTPS unless explicitly promoted after v1
- enterprise DNS policy engine
- replacing existing DNS servers
- making core transport depend on DNS helper availability
