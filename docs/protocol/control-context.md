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

Relay routing uses outer routing/relay-hop headers and authenticated relay
session state; endpoint service/exit decisions use authenticated
service/control context only after endpoint decrypt.

## Discovery Vs Remote Status

Discovery and remote IPC/status are different control-plane behaviors.

Discovery is continuous and sparse. It uses authenticated control metadata to
advertise stable facts such as service id/name mappings, path names, capacity,
MTU, disabled-service assertions, endpoint assertions for verification, and
compact path-pressure summaries. Path-pressure summaries are receiver feedback:
loss, queue, send-failure, reorder, sender in-flight, predicted delivery,
socket-buffer, and drop facts that Python may use to recompile scheduler
credits or path health. They are not user-payload
acknowledgements and do not add retransmission semantics to ordinary UDP. The
cadence should stay low at baseline and send promptly when an important fact
changes. Discovery is cheap enough to keep on, but it must not become a
continuous live-status stream.

Remote IPC/status is explicit, louder, temporary, and read-only. It uses the
remote-status reserved service lane when an operator or local tool asks for a
peer status snapshot or short-lived status stream. The requester refreshes the
request while it is still interested; the peer stops sending remote status when
the request expires. If no fresh response is available, operator views should
show the remote status as stale or unknown rather than pretending the remote
service is locally registered.

Remote status is also the first explicit internal metadata ack/retry surface.
Each request carries a request id, the requester tracks pending ids, and a
matching response acknowledges that internal metadata request. Timed-out pending
requests are counted locally and a later request may be sent while the operator
interest window is still active. This reliability behavior is deliberately
limited to the reserved remote-status service lane; it must not create hidden
retransmission or delivery promises for ordinary user UDP payloads.

Learned remote services are read-only discovered facts. They may appear in
operator listings as remote entries, but the local service registry remains the
source of truth for local process lifecycle. Discovery and remote status must
not allow remote start, stop, reload, endpoint changes, trust-root changes, or
other mutations.

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
