# NAT Traversal Philosophy

## Purpose

Gatherlink should handle common NAT realities without becoming a full P2P NAT
traversal framework too early.

## Initial model

The current deployment model assumes the client initiates to a reachable relay/server and that the
relay/server has a reachable endpoint.

## NAT rebinding

Runtime should tolerate source port changes, source address changes, mobile
carrier NAT changes, and path-specific rebinding. Rebinding must be
authenticated before path state is accepted.

## STUN/TURN

STUN/TURN-style behavior is future scope. If added, keep it as a helper/control
plane feature, not a core requirement.

## Hosted relays

Hosted relays are the clean commercial and operational answer for most hard NAT
cases.

## Security

Never accept unauthenticated path rebinding.
