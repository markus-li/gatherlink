# Relay Session Lifecycle

## Purpose

Secure relay forwarding uses explicit encrypted relay-hop sessions. Gatherlink
does not support plaintext routing labels for secure transport.

Relays are allowed to forward only packets that authenticate under a relay-hop
session they were configured to accept.

Routing through untrusted peers is represented by the outer routing/relay-hop
header plus authenticated relay session state. `route_id` is explicitly removed
and must not be used as a packet, runtime DTO, or compatibility routing field.

## Relay session provisioning

The first implementation should provision relay sessions from signed topology
or authenticated control context.

Dynamic relay-session requests may come later, but they must still result in
explicit authenticated relay session state before data packets are forwarded.

Relay session state should include:

- relay receiver index
- upstream authenticated peer identity
- downstream next hop/session
- direction
- generation id
- expiry
- revocation/topology generation
- allowed packet type
- optional max packet size
- optional byte, packet, and concurrent-flow limits

The relay does not need endpoint `service_id`, endpoint `path_id`, endpoint
payload, endpoint address, tenant name, or plaintext route metadata.

The first implementation models this as Python-owned
`RelaySessionAuthorization` state. Python validates topology membership, relay
role, revocation, direction, expiry, packet-size limits, receiver indexes, and
next-hop identity before any compact runtime DTO is produced. Rust should only
execute the resulting relay-hop packet checks and counters; it must not infer
relay authorization from plaintext labels.

The current v1 implementation has the first half of that split:

- Python `RelaySessionAuthorization` validates signed-topology relay policy and
  exports socket-ready `RelayExecutorConfig` records with receiver index, expiry,
  UDP next-hop address, next-hop receiver index, direction, generation, and
  optional limits.
- Expired relay authorizations are omitted from the compiled executor set, so
  stale relay state fails closed before Rust receives it.
- Rust `RelaySessionExecutor` accepts only those compiled facts and checks
  receiver index, expiry, packet size, packet limits, byte limits, and counters.
- Rust also has a compact hop AEAD outer-envelope unwrap/reseal primitive for
  an already authorized relay-hop session. It authenticates/decrypts only the
  hop envelope, applies the compiled checks, and reseals the same opaque
  endpoint-encrypted packet bytes for the next hop receiver index without
  learning endpoint service or routing meaning.
- Rust has a narrow compiled next-hop UDP forwarding primitive for this relay
  executor. Python still chooses the next-hop endpoint, keys, expiry, limits,
  and authorization; Rust only polls the relay socket, drops invalid packets
  without a network response, reseals valid opaque bytes, and sends to the
  compiled next-hop address.
- The executor config deliberately omits endpoint `service_id`, endpoint
  `path_id`, route labels, and payload meaning.

Python orchestration now has foreground and process-managed relay runners for
the compiled Rust primitive. The runner loads one already-authorized relay
executor config, binds the Rust hop forwarder, drains local diagnostics, exposes
service IPC status/stop, and reports lifecycle/counter facts. It still does not
authorize topology, choose peers, decode endpoint payloads, or route by
plaintext labels in Rust. Multi-session relay supervision can build on the same
config shape by launching one or more compiled relay-hop services.

## Forwarding rules

On receive, the relay:

```text
lookup relay_receiver_index
if unknown: silent drop
check relay AEAD
if auth fails: silent drop
check relay replay window
if replay: silent drop
check direction, expiry, generation, limits, authorization
if invalid: silent drop
reseal opaque endpoint packet bytes and send to Python-compiled next hop/session
```

Every failure increments local counters and may emit rate-limited diagnostics.
Invalid packets must not trigger responses to the network.

## Discovery and validation

Unsigned relay discovery may produce candidates only.

Before a relay is used for secure forwarding, the candidate must be validated by
signed topology, trust-root policy, or authenticated control context. Discovery
metadata must never be enough by itself to authorize transit.

## Relay and exit roles

Relay and exit are separate policy roles.

One node may have both roles, but authorization should keep them distinct:

- relay role forwards an authenticated inner packet without decrypting endpoint
  payload
- exit role decrypts endpoint traffic for a service it is authorized to serve

This distinction keeps untrusted relay peers from learning endpoint service
contents or making endpoint routing decisions.

## Expiry and revocation

Relay sessions should be short-lived enough for topology and revocation updates
to take effect without waiting for a long data-plane lifetime.

Handshake/control setup must check the current signed topology and revocation
generation. Later versions may add explicit signed revocation lists, but the
initial model should be generation-based.

## Diagnostics

Relay diagnostics should use stable event codes, including:

- `relay.auth_failed`
- `relay.replay_drop`
- `relay.unknown_receiver_index`
- `relay.unauthorized_next_hop`
- `relay.expired_session`
- `relay.generation_stale`
- `relay.limit_exceeded`
- `relay.packet_too_large`

Diagnostics are local. The relay remains stealth on the network for invalid
packets.
