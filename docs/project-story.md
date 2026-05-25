# Project Story

Gatherlink began as a practical need: make a small site use more than one
connection without turning the whole machine into a router, firewall, or
traditional VPN appliance.

The first real target was deliberately concrete: aggregate a fiber connection
and a 5G connection while keeping ordinary Linux tools in their normal roles.
That shaped the design more than any abstract product category. Gatherlink
should carry UDP services across several paths, explain what it is doing, and
stay out of domains already owned by WireGuard, DNS servers, firewalls, routers,
and operating-system network policy.

## Design Approach

The project was designed from the boundaries inward.

Rust owns the packet executor because packet parsing, encryption, replay checks,
dedupe, fragmentation, queues, and counters need to be compact and fast. Python
owns meaning because config, helper behavior, provisioning, diagnostics,
scheduling policy, release tooling, and operator language need to stay explicit
and easy to change.

That split is the core architecture. It keeps the fast path small while letting
the control plane remain understandable.

## Corrections Along The Way

Several early ideas were intentionally removed or narrowed as the design became
clearer:

- routing labels were removed from data packets; routing through untrusted
  peers uses authenticated outer relay/session context instead
- packet headers were made compact instead of carrying fields that can be
  derived from session context after authentication
- helpers stayed outside the core so SOCKS5, DNS, WireGuard, TCP forwarding,
  time sync, and status HTTP could be useful without becoming packet semantics
- lab code was kept honest: a lab may prove behavior, but production code must
  own the behavior
- roadmaps were split between closed release history, the active release, and
  future ideas so deferred work does not pretend to be implemented

These corrections are as important as the features. They show the project
choosing a smaller and cleaner shape when the broader path would have been
easier to justify in the moment.

## Security Posture

Gatherlink's security posture is inspired by the WireGuard protocol: quiet
public receive behavior, compact fixed packet classes, opaque receiver indexes,
no unauthenticated negotiation, replay windows, key rotation, and a small
auditable packet surface.

It is not WireGuard-compatible and does not try to become a VPN interface.
WireGuard remains the right tool for VPN semantics. Gatherlink focuses on
carrying configured UDP services over multiple carrier paths and, when useful,
carrying WireGuard itself as one of those services.

## Validation Culture

The project treats documentation, tests, labs, and VM acceptance as part of the
design rather than paperwork after the fact.

The expected loop is:

1. define the boundary
2. implement the smallest useful slice
3. test it with unit tests and realistic labs
4. prove important behavior in VMs
5. update the canonical docs
6. move unfinished work to the correct roadmap

This is why the repository has a strong documentation map, a living assessment,
closed release roadmaps, active release planning, and future design parking.
Those documents are not decoration; they are how the project avoids becoming a
pile of clever pieces.

## What This Project Shows

Gatherlink is a showcase of backend architecture more than frontend polish:

- disciplined ownership boundaries
- security-sensitive protocol design
- compact packet thinking
- practical Linux operations
- testable helper boundaries
- realistic lab and VM validation
- willingness to remove stale assumptions

The goal is not to make the largest possible network product. The goal is to
make a small, sharp transport that can be understood, operated, and extended
without losing its shape.
