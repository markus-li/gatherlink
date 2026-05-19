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
forward authenticated inner packet to configured next hop/session
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

Diagnostics are local. The relay remains stealth on the network for invalid
packets.
