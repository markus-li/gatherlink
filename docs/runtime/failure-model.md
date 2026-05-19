# Failure Model

## Purpose

Transport systems become usable when failures are predictable, observable, and
recoverable.

## Failure categories

Gatherlink should distinguish down, degraded, invalid, disabled, flapping,
recovering, overloaded, and unknown states.

## Path failure

A path may fail because an interface is down, an IP was removed, a gateway is
unreachable, a route is ambiguous, a carrier is blocked, a peer is unreachable,
an authenticated probe fails, MTU probing fails, queue age exceeds policy, or
receiver metrics show degradation.

Behavior: remove from scheduler, keep watching for recovery, emit event, run
hooks if configured, and do not crash the service.

## Carrier failure

A carrier profile may fail independently of the physical link. Example: raw UDP
blocked while WSS works. Mark the logical path down/degraded, retry according to
carrier discovery policy, and keep other logical paths on the same physical link
alive.

## Overload

Overload must not cause unbounded memory growth. Use bounded queues for paths,
diagnostics, hooks, DNS work, bootstrap attempts, carrier discovery, replay
windows, reorder buffers, metrics history, and logs.

## Stream-like carrier stall

WSS/TCP/QUIC paths may stall due to head-of-line blocking or remote backpressure.
Never block the global dataplane; queue per path only and expose queue age/depth.

## Peer failure

Do not switch peer because one carrier failed. Switch peer when the peer as a
whole is unreachable or materially worse. Failover can be aggressive; failback
should be conservative.

## Metrics failure

If receiver metrics stop arriving, fall back to local metrics, reduce confidence,
do not assume the path is good, and emit diagnostics.

## DNS/helper/time/hook failures

Optional helpers must fail independently. Core transport should keep running
where possible and expose clear status.

Secure transport fails closed by default. Authentication failure, replay,
unauthorized relay direction, stale generation, and revoked topology state must
drop the packet or session rather than falling back to plaintext or guessing.

Helper failure degrades only that helper unless policy explicitly says the
service depends on it.

DNS helper behavior:

- validated stale answer available: serve stale with diagnostics if policy
  allows stale
- no valid stale answer: return SERVFAIL
- policy denied: return REFUSED
- DNSSEC bogus under a validating policy: return failure and emit
  `dns.dnssec_bogus`

Relay failure behavior:

- relay fabric health updates candidate state
- sessions may retry an alternate relay if authenticated policy allows
- invalid relay packets are silent drops
- no fail-open to plaintext routing

Time helper failure behavior:

- warn only
- do not stop internal monotonic/session time
- do not set system time unless the helper is explicitly configured to do so

Interactive tools may prompt for dangerous one-shot actions. Services must not
block waiting for operator input; they should fail closed or continue degraded
according to policy.

## Safe mode

A future safe mode should disable helpers, disable overlay routing, disable
adaptive scheduling, use conservative MTU, use the single best verified path,
and keep diagnostics available.
