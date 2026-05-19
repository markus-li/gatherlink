# Diagnostics Dictionary

This file maps common counters and event codes to operator meaning. It does not
replace structured diagnostics; it explains what to check next.

## Core Counters

| Counter family | Meaning | What to check |
| --- | --- | --- |
| TX packets/bytes increasing | local service is sending into Gatherlink | sink counters, carrier reachability |
| RX packets/bytes increasing | packets are reaching this node | delivery counters and target service |
| delivered packets increasing | decrypted/reassembled payloads are emitted | application-side behavior |
| expected duplicates increasing | fanout copies are arriving and being discarded | normal when duplicate paths are configured |
| duplicate packets increasing | dedupe/replay-like duplicate handling is active | path fanout, retries, replays |
| send failures increasing | local socket send failed | path endpoint, interface, permissions, firewall |
| missed packets increasing | sequence gaps or loss are visible | path loss, shaping, MTU, overload |
| reorder-needed increasing | packets arrived out of order | path jitter/reorder, fanout timing |

Names vary by view, but they should be derived from structured runtime status,
not parsed prose.

## Crypto And Stealth Drops

| Event code | Meaning | Expected wire behavior |
| --- | --- | --- |
| `crypto.auth_failed` | packet failed transport authentication | silent drop |
| `crypto.replay_drop` | packet was too old or already seen | silent drop |
| `crypto.unknown_receiver_index` | receiver index is not accepted locally | silent drop |

Do not try to make these errors visible to the sender. Stealth receive means the
local node records operator-safe counters while invalid packets get no network
response.

## Relay Events

| Event code | Meaning | What to check |
| --- | --- | --- |
| `relay.auth_failed` | relay-hop AEAD failed | relay session keys and generation |
| `relay.replay_drop` | relay-hop packet replayed | sender replay state and duplicates |
| `relay.unknown_receiver_index` | relay session not found | provisioning and receiver lifecycle |
| `relay.unauthorized_next_hop` | packet tried an unauthorized hop | relay authorization config |
| `relay.expired_session` | relay session exceeded expiry | rekey/provisioning cadence |
| `relay.generation_stale` | packet generation is old | stale config or old sender |
| `relay.limit_exceeded` | relay rate/byte/packet limit hit | configured relay limits |
| `relay.packet_too_large` | relay packet exceeds allowed size | MTU and fragmentation policy |

Relays are untrusted and must not route by plaintext labels. Invalid relay
packets must not be forwarded.

## Runtime Events

| Event code | Meaning | What to check |
| --- | --- | --- |
| `service.bound` | a UDP service bound successfully | listen address and target |
| `runtime.start_failed` | foreground or managed service failed startup | config validation and log |
| `runtime.shutdown` | service exited intentionally | close command or stop reason |
| `config.reapplied` | Python reapplied runtime config | generation and changed fields |
| `counter.snapshot` | structured counter sample | trends, not one-off messages |
| `diagnostics.queue_dropped` | diagnostics could not keep up | sink speed and event volume |

Diagnostics must not block packet forwarding. Dropped diagnostics are a local
observability issue, not a packet-path failure by themselves.

## Helper Events

| Event code | Meaning | What to check |
| --- | --- | --- |
| `helper.lifecycle.started` | supervisor started a helper process | service registry and helper log |
| `helper.lifecycle.start_failed` | helper process could not start | command, bind address, permissions |
| `helper.stream.opened` | helper stream opened | expected client activity |
| `helper.stream.closed` | helper stream closed | normal close or timeout |
| `helper.stream.denied` | stream policy denied request | allow host/port policy |
| `helper.stream.unreachable` | helper could not reach target | exit connectivity and target service |
| `helper.stream.invalid_frame` | helper stream framing failed | version mismatch or corrupt stream |
| `socks.exit_denied` | SOCKS exit policy denied request | SOCKS allow list |
| `socks.exit_unreachable` | SOCKS exit could not connect | remote target, DNS, firewall |
| `helper.wireguard.plan` | WireGuard helper rendered a plan | WireGuard endpoint mapping |
| `helper.time.set_failed` | time helper could not set system time | permissions, NTP conflict |
| `helper.status_http.started` | status HTTP helper started | local bind and write window |
| `helper.status_http.non_loopback_bind` | status helper bound outside loopback | explicit danger flag and exposure |

Helpers remain Python-owned. Helper diagnostics explain helper behavior without
turning helpers into core protocol features.

## DNS Events

| Event code | Meaning | What to check |
| --- | --- | --- |
| `dns.policy_denied` | DNS policy rejected a query | domain sets and rule order |
| `dns.upstream_failed` | configured upstream failed | upstream address, timeout, transport |
| `dns.dnssec_bogus` | DNSSEC policy rejected answer | upstream AD bit or validation policy |

Direct DNS upstreams run now. Gatherlink-tunnel DNS upstream execution remains a
v0.9 follow-up until implemented and VM-proven.

## Reading JSONL

Each diagnostics line should be one redacted JSON event with:

- `schema_version`
- `timestamp`
- `code`
- `kind`
- `severity`
- optional `node`, `service`, `path`, `helper`, and `peer`
- structured `details`

Operator text may change. Codes and structured fields should remain stable
enough for scripts and future UI/API views.
