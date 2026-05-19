# Control Context

## Purpose

Control context is authenticated runtime state learned or refreshed over a
verified Gatherlink relationship. It tells the dataplane how compact runtime ids
map to services, paths, helper commands, relay-hop sessions, and local policy.

## Trust boundary

Control context should be considered authenticated state when it arrives inside
an encrypted and verified peer session.

If encryption is disabled for a lab, control context is not trusted. Plaintext
mode must be loud in diagnostics and must not be used for production routing,
relay authorization, or helper automation that changes the host.

Even authenticated control context must not change peer IP/port endpoint config.
Endpoint assertions may verify that the expected peer is reachable, but changing
configured endpoints requires a separate signed configuration distribution
mechanism.

## Ownership

Python owns control-message parsing, validation, policy semantics, and compiled
runtime state.

Rust receives only compact execution state that is already validated. Rust does
not decide whether a service should exist, where it exits, whether a helper is
allowed, or whether an authenticated assertion changes policy.

## Generations

Control messages that update runtime mappings must carry a generation id.

Receivers must ignore stale generations. When possible, updates should be
idempotent so a re-sent current generation converges to the same runtime state.

Suggested generation behavior:

- signed topology/provisioning bundle defines the base generation
- control messages may refine runtime state inside that generation
- newer generation replaces older state for the same scope
- stale generation is ignored with a local diagnostic counter
- impossible or unauthorized generation jump is rejected

## Service mapping

`service_id` is the main compact runtime selector after decryption. It maps to a
service definition through authenticated config/control context.

The mapping may define:

- local bind or exit behavior
- allowed peer identities
- allowed helper behavior
- path/scheduler policy
- relay/exit role
- diagnostics labels

A service can only exit where authenticated policy allows. If a service may exit
through multiple peers, that must be explicit in control/config state rather
than inferred from plaintext packet fields.

`route_id` is not part of control context and should not be preserved as a
compatibility field. Relay routing uses outer routing/relay-hop headers and
authenticated relay session state; endpoint service/exit decisions use
authenticated service/control context only after endpoint decrypt.

## Helper control

Helper-related control messages should use reserved service ids.

Small helper commands may share a common helper-control service wrapper. That
wrapper should carry helper kind, command id, generation, and compact payload.

Heavier helper traffic, streaming helper data, or helper protocols with distinct
lifetime and flow-control needs should use a dedicated reserved service id.

Examples:

- DNS helper cache/policy hints may use the shared helper-control wrapper
- time-quality assertions may use the shared helper-control wrapper
- SOCKS5 proxied streams use their own service
- WireGuard orchestration uses its own helper/service mapping when needed

## Security limits

Control context may authorize compact ids and local runtime behavior. It must
not:

- silently change configured peer endpoints
- install new trust roots without signed config distribution
- grant data-plane access from a bootstrap token alone
- override revocation state
- make plaintext routing labels trustworthy
- turn relays into payload inspection points

Failures should be fail-closed for secure transport and visible through local
rate-limited diagnostics.
